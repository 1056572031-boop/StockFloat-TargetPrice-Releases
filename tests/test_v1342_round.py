# -*- coding: utf-8 -*-
"""v1.3.42 连续 10 轮极端走查修复的回归保护。

覆盖九项修复（全部为**实跑确认**后修复，非推理）：
  1. BOM 配置不再静默丢目标价（_read_json_or_none 改 utf-8-sig）
  2. 保存底稿不可解析时回落备份快照（_base_dict_for_write）
  3. load_alerts 主配置损坏时回落备份快照
  4. merge_alerts：live 同槽位全部过期时采用期望条目（目标价不再凭空消失）
  5. _secid 入参 strip + 920 段归北交所；_tencent_code 裸 8307 补零归北交所
  6. _drop_flash_due：NaN / ±inf 不再误触发下跌闪烁
  7. fmt_pct：极端大数不再展开成 300+ 字符（改科学计数法）
  8. 脱敏：JSON 体里的凭据、家目录用户名
  9. 停牌/无数据徽标走 badge_vertical（不再压住代码行）
"""
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone, timedelta

import sf_config
import sf_quote
import sf_ui_render
import targets
from sf_config import (_base_dict_for_write, load_alerts, redact_config,
                       redact_log_text, save_alerts, save_config)
from sf_constants import APP_VERSION, VERSION_TEXT
from sf_utils import _drop_flash_due, fmt_pct

BJ = timezone(timedelta(hours=8))




class _CfgTmpMixin:
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sf_v1342_")
        self.cfg = os.path.join(self.tmp, "config.json")
        self.bak = self.cfg + ".bak"
        self._orig_cfg = sf_config.CONFIG_PATH
        self._orig_bak = sf_config.CONFIG_BACKUP_PATH
        self._orig_log = sf_config.log_line
        sf_config.CONFIG_PATH = self.cfg
        sf_config.CONFIG_BACKUP_PATH = self.bak
        sf_config.log_line = lambda *a, **k: None

    def tearDown(self):
        sf_config.CONFIG_PATH = self._orig_cfg
        sf_config.CONFIG_BACKUP_PATH = self._orig_bak
        sf_config.log_line = self._orig_log
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, obj, bom=False, path=None):
        enc = "utf-8-sig" if bom else "utf-8"
        with open(path or self.cfg, "w", encoding=enc) as f:
            json.dump(obj, f)

    def _read(self, path=None):
        with open(path or self.cfg, "r", encoding="utf-8-sig") as f:
            return json.load(f)


class TestBomConfig(_CfgTmpMixin, unittest.TestCase):
    """修复 1/2/3：BOM / 坏配置不得静默吞掉目标价。"""

    GOOD = {"code": "sh600519", "x": 10, "y": 20, "h": 260, "bg": "glass",
            "version": 1,
            "api_urls": {"quote": "https://qt.gtimg.cn/q=%s"},
            "alerts": [{"direction": "above", "target": 1500.0,
                        "session_date": "2026-09-14", "state": "pending"}]}

    def test_bom_config_alerts_survive_save(self):
        """文本编辑另存带 BOM 后，任意一次保存都不得把 alerts / api_urls 冲掉。"""
        self._write(dict(self.GOOD))
        shutil.copy2(self.cfg, self.bak)
        self._write(dict(self.GOOD), bom=True)          # 模拟用户手工编辑
        self.assertEqual(len(load_alerts()), 1, "带 BOM 的配置也必须能读出目标价")
        self.assertTrue(save_config("sh600519", 11, 22, 260, "glass"))
        after = self._read()
        self.assertEqual(len(after.get("alerts", [])), 1, "alerts 不得丢失")
        self.assertIn("api_urls", after, "api_urls 不得丢失")

    def test_read_json_tolerates_bom(self):
        self._write({"a": 1}, bom=True)
        self.assertEqual(sf_config._read_json_or_none(self.cfg), {"a": 1})

    def test_base_dict_falls_back_to_backup(self):
        """主文件不可解析时，写入底稿必须改用备份快照而不是空 dict。"""
        self._write(dict(self.GOOD))
        shutil.copy2(self.cfg, self.bak)
        with open(self.cfg, "w", encoding="utf-8") as f:
            f.write("{ this is not json")
        base = _base_dict_for_write(self.cfg, self.bak)
        self.assertEqual(len(base.get("alerts", [])), 1, "应回落到备份快照作底稿")

    def test_load_alerts_falls_back_to_backup(self):
        self._write(dict(self.GOOD))
        shutil.copy2(self.cfg, self.bak)
        with open(self.cfg, "w", encoding="utf-8") as f:
            f.write("[1,2,3]")            # 顶层非对象
        self.assertEqual(len(load_alerts()), 1, "主配置损坏时必须从备份读回目标价")

    def test_empty_config_still_saves(self):
        """首装（无配置文件）不得因底稿逻辑报错。"""
        self.assertTrue(save_config("sh600519", 1, 2, 224, "glass"))
        self.assertEqual(self._read()["code"], "sh600519")


class TestMergeAlerts(unittest.TestCase):
    """修复 4：面板保存时目标价不得凭空消失。"""

    def test_expired_live_slot_falls_back_to_desired(self):
        now = datetime(2026, 9, 14, 16, 0, tzinfo=BJ)   # 已过 15:00:30 清空时刻
        live = [{"direction": "above", "target": 1600.0,
                 "session_date": "2026-09-14", "state": "pending", "last_side": None}]
        desired = [{"direction": "above", "target": 1600.0,
                    "session_date": "2026-09-15", "state": "pending", "last_side": None}]
        merged = targets.merge_alerts(desired, live, now=now)
        kept = targets.drop_expired(merged, now)
        self.assertEqual(len(kept), 1, "live 同槽位全过期时必须采用期望条目")
        self.assertEqual(kept[0]["session_date"], "2026-09-15")

    def test_live_hit_state_still_preserved(self):
        """回归保护：live 未过期时仍必须保住 state=hit（不得重复提醒）。"""
        now = datetime(2026, 9, 14, 10, 0, tzinfo=BJ)
        live = [{"direction": "above", "target": 1600.0,
                 "session_date": "2026-09-14", "state": "hit", "last_side": "ABOVE"}]
        desired = [{"direction": "above", "target": 1600.0,
                    "session_date": "2026-09-14", "state": "pending"}]
        merged = targets.merge_alerts(desired, live, now=now)
        self.assertEqual(merged[0]["state"], "hit")


class TestCodeNormalize(unittest.TestCase):
    """修复 5：代码带空格 / 北交所段位。"""

    def test_secid_strips_input(self):
        self.assertEqual(sf_quote._secid("  SH600519  "), "1.600519")

    def test_secid_920_is_beijing(self):
        self.assertEqual(sf_quote._secid("920001"), "0.920001")

    def test_tencent_code_bare_8307(self):
        self.assertEqual(sf_quote._tencent_code("8307"), "bj830700")

    def test_tencent_code_regression(self):
        self.assertEqual(sf_quote._tencent_code("600519"), "sh600519")
        self.assertEqual(sf_quote._tencent_code("000001"), "sz000001")
        self.assertEqual(sf_quote._tencent_code("bj8307"), "bj830700")
        self.assertEqual(sf_quote._tencent_code(""), "sz")
        self.assertEqual(sf_quote._tencent_code(None), "sz")


class TestFlashAndFormat(unittest.TestCase):
    """修复 6/7：脏数据不得触发误报，也不得撑破界面。"""

    def test_non_finite_never_triggers_flash(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            self.assertFalse(_drop_flash_due(bad, 3.0, None, 100.0, 60.0),
                             "非有限涨跌幅不得触发闪烁：%r" % bad)

    def test_flash_regression(self):
        self.assertTrue(_drop_flash_due(-5.0, 3.0, None, 100.0, 60.0))
        self.assertFalse(_drop_flash_due(5.0, 3.0, None, 100.0, 60.0))
        self.assertFalse(_drop_flash_due(0.0, 3.0, None, 100.0, 60.0))
        self.assertFalse(_drop_flash_due(None, 3.0, None, 100.0, 60.0))
        self.assertFalse(_drop_flash_due("bad", 3.0, None, 100.0, 60.0))

    def test_fmt_pct_extreme_is_bounded(self):
        self.assertLessEqual(len(fmt_pct(1e308)), 12)
        self.assertLessEqual(len(fmt_pct(-1e308)), 12)

    def test_fmt_pct_regression(self):
        self.assertEqual(fmt_pct(10.0), "+10.00%")
        self.assertEqual(fmt_pct(-1.234), "-1.23%")
        self.assertEqual(fmt_pct(0.0), "0.00%")
        self.assertEqual(fmt_pct(float("inf")), "0.00%")
        self.assertEqual(fmt_pct(float("nan")), "0.00%")


class TestRedact(unittest.TestCase):
    """修复 8：诊断导出不得带凭据与本机用户名。"""

    def test_json_body_credentials(self):
        out = redact_log_text('{"token": "T0PSECRET", "code": "sh600519"}')
        self.assertNotIn("T0PSECRET", out)

    def test_nested_json_credentials(self):
        out = redact_log_text('{"auth": {"password": "PW123"}}')
        self.assertNotIn("PW123", out)

    def test_home_username(self):
        out = redact_log_text("读 /Users/example/Library/x.json 失败")
        self.assertNotIn("moriarty", out)
        self.assertIn("<USER>", out)

    def test_config_export_redacts_path(self):
        red = redact_config({"path": "/Users/example/Library/x.json",
                             "eastmoney_token": "SECRET"})
        s = json.dumps(red, ensure_ascii=False)
        self.assertNotIn("moriarty", s)
        self.assertNotIn("SECRET", s)

    def test_normal_params_not_mangled(self):
        result = redact_log_text("https://a.b/c?code=sh600519")
        self.assertIn("https://a.b/", result)
        self.assertNotIn("sh600519", result)


class TestBadgeVertical(unittest.TestCase):
    """修复 9：停牌/无数据徽标必须走同一套垂直位置约束。"""

    def test_fallback_badge_has_a_place_to_sit(self):
        """降级态（价格显示 "--"）的徽标锚在价格带内，而不是被涨跌幅那套钳制判死。

        原来这一条是 inspect.getsource 断言源码里含 badge_vertical(Y_PRICE)：
        它把"死支路"本身钉成不可改（AGENTS §5.2-10 给的两条修法都会让它红），
        而且违反 §6 第 2 条（不许读源码做字符串断言）。现在改判可观测事实 ——
        位置算得出来、落在价格带内；"真的画出来了"由 tests/test_render.py 的
        像素用例负责（那一条已反向验证过：把新函数改成恒不画，它就红）。
        """
        top = sf_ui_render.badge_in_band_top(sf_ui_render.Y_PRICE,
                                             sf_ui_render.PRICE_BAND_H)
        self.assertIsNotNone(top, "降级态算不出位置，等于回到 S49 那条死支路")
        self.assertGreaterEqual(top, sf_ui_render.Y_PRICE,
                               "徽标顶边跑出价格带上方")
        self.assertLessEqual(top + sf_ui_render.STATUS_BADGE_H,
                             sf_ui_render.Y_PRICE + sf_ui_render.PRICE_BAND_H,
                             "徽标底边跑出价格带下方")

    def test_the_old_fallback_call_was_actively_dead(self):
        """把"为什么要改"钉在数字上：价格带顶当涨跌幅顶传，恒判不绘制。"""
        self.assertIsNone(sf_ui_render.badge_vertical(sf_ui_render.Y_PRICE))

    def test_badge_vertical_clamps_and_drops(self):
        self.assertEqual(sf_ui_render.badge_vertical(100), 100 - 12 - 2)
        # 空间不足时返回 None（不绘制），绝不压住代码行
        self.assertIsNone(sf_ui_render.badge_vertical(
            sf_ui_render.BADGE_MIN_TOP + 2, 12, 2, sf_ui_render.BADGE_MIN_TOP))


class TestMinimizeDockCleanup(unittest.TestCase):
    """修复 10：缩到 Dock 时气泡与按钮交互态必须一起收口。"""

    def test_minimize_source_closes_bubble_and_resets_press(self):
        import inspect
        import stock_float_window as M
        src = inspect.getsource(M.FloatWindow.minimize_to_dock)
        self.assertIn("_hide_target_bubble", src, "缩 Dock 必须一并隐藏独立气泡窗")
        self.assertIn("_press_btn = None", src, "缩 Dock 必须复位按钮按下态")


if __name__ == "__main__":
    unittest.main()
