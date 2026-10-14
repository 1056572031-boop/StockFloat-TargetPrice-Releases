# -*- coding: utf-8 -*-
"""v1.4.18 连续走查修复的行为回归（不发布，仅守护本轮改动）。

覆盖四处修复，全部断言**可观测行为**，不读源码做字符串断言：
  S1  切股后目标价判定价必须归零，不得沿用上只股票的 _real_price
  #1  urllib 主路径（连接池关闭/pooled=False）遇到不可重试的 4xx 只发一次请求
  #2  崩溃报告关键段抽取对畸形 usedImages 绝不抛异常（守"不抛异常"契约）
  #4  诊断导出对键名恰为 key 的字段同样脱敏（与网络层同一安全口径）
"""
import base64
import hashlib
import json
import os
import sys
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from nacl.signing import SigningKey

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtWidgets import QApplication

import sf_config
import sf_constants
import sf_crashreport
import sf_network
import sf_quote
import sf_update
import sf_utils
import stock_float_window as m


def _fake_http_get(url, **kw):
    return 'v_sh600519="1~贵州茅台~600519~1510.0~1490.0~0.1~1.01~10.5~9.8";'


class TestRedactKeyToken(unittest.TestCase):
    def test_key_named_field_is_redacted(self):
        # 与 sf_network._SENSITIVE_QUERY_KEYS 保持同一口径：键名叫 key 也要遮。
        out = sf_config.redact_config({"api": {"key": "SECRETVALUE"}})
        self.assertNotIn("SECRETVALUE", json.dumps(out, ensure_ascii=False))
        self.assertEqual(out["api"]["key"], sf_config._REDACTED)

    def test_benign_field_survives(self):
        out = sf_config.redact_config({"code": "sh600519", "height": 200})
        self.assertEqual(out["code"], "sh600519")
        self.assertEqual(out["height"], 200)


class TestCrashReportNeverRaises(unittest.TestCase):
    def _ips(self, used_images):
        obj = {"exception": {"type": "EXC_CRASH", "signal": "SIGSEGV"},
               "threads": [{"id": 1, "frames": [{"imageIndex": 0, "symbol": "main"}],
                            "triggered": True}],
               "usedImages": used_images}
        # 以 "{" 开头才会进入 JSON 渲染分支（真实 .ips 首行即元数据 JSON）。
        return json.dumps(obj)

    def test_null_base_does_not_raise(self):
        # base=null → int(None) 曾抛 TypeError，冒泡后整段证据从诊断包消失。
        text = self._ips([{"name": "StockFloat", "base": None, "arch": "arm64"}])
        out = sf_crashreport.extract_crash_key_sections(text)
        self.assertIsInstance(out, str)   # 不抛即通过：允许退回纯文本兜底

    def test_non_dict_image_does_not_raise(self):
        text = self._ips(["oops", {"name": "x", "base": 1, "arch": "arm64"}])
        out = sf_crashreport.extract_crash_key_sections(text)
        self.assertIsInstance(out, str)

    def test_valid_report_still_renders_images(self):
        text = self._ips([{"name": "StockFloatTargetPrice", "base": 4300000000,
                           "arch": "arm64"}])
        out = sf_crashreport.extract_crash_key_sections(text)
        self.assertIn("Key Images", out)


class TestUrllibNoRetryOnRejection(unittest.TestCase):
    def test_403_issued_once_in_urllib_path(self):
        calls = {"n": 0}

        def _raise_403(req, timeout=None):
            calls["n"] += 1
            raise urllib.error.HTTPError(req.full_url if hasattr(req, "full_url") else "u",
                                         403, "Forbidden", {}, None)

        with patch.object(sf_network.time, "sleep", lambda s: None), \
                patch("sf_network.safe_urlopen", _raise_403):
            with self.assertRaises(urllib.error.HTTPError):
                # pooled=False → pool_attempted=False → fb_retries 用满 retries，
                # 正是连接池被关闭时的 urllib 主路径。
                sf_network.http_get("https://example.invalid/x", pooled=False, retries=2)
        self.assertEqual(calls["n"], 1,
                         "403 明确拒绝不该在 urllib 路径重试，实际发次=%d" % calls["n"])

    def test_503_still_retried_in_urllib_path(self):
        calls = {"n": 0}

        def _raise_503(req, timeout=None):
            calls["n"] += 1
            raise urllib.error.HTTPError("u", 503, "Unavailable", {}, None)

        with patch.object(sf_network.time, "sleep", lambda s: None), \
                patch("sf_network.safe_urlopen", _raise_503):
            with self.assertRaises(urllib.error.HTTPError):
                sf_network.http_get("https://example.invalid/x", pooled=False, retries=2)
        self.assertEqual(calls["n"], 3, "5xx 仍应重试（retries=2 → 共 3 次）")


class TestApplyStockResetsPrice(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._patches = [
            patch.object(m, "load_config",
                         return_value=("sh600519", 100, 100, 200, "translucent")),
            patch.object(m, "save_config", lambda *a, **k: None),
            patch.object(sf_network, "http_get", _fake_http_get),
            patch.object(m, "start_icon_worker", lambda: None),
            patch.object(m.FloatWindow, "_force_top", lambda self: None),
            patch.object(m.FloatWindow, "_setup_reopen_handler", lambda self: None),
            patch.object(m.FloatWindow, "_force_fetch", lambda self: None),
        ]
        for p in self._patches:
            p.start()
        self.win = m.FloatWindow()
        self.win.running = False
        self.win._wake.set()
        self.win.timer.stop()
        self.win.top_timer.stop()
        self.win.show()
        QApplication.processEvents()

    def tearDown(self):
        self.win.close()
        QApplication.processEvents()
        for p in self._patches:
            p.stop()

    def test_switching_stock_clears_judged_price(self):
        # 收盘后/熔断场景：上一只股票的判定价残留会让 _current_price() 返回别家价格。
        self.win._real_price = 1500.0
        self.win._price_is_fallback = True
        self.win._anim_driver.anim_to_price = 1500.0
        self.win._anim_driver.anim_from_price = 1500.0
        self.assertEqual(self.win._current_price(), 1500.0)
        self.assertTrue(self.win.apply_stock("sz000001"))
        self.assertEqual(self.win._current_price(), 0.0,
                         "切股后判定价必须归零，等新股真实行情")
        self.assertTrue(self.win._price_is_fallback)
        self.assertEqual(self.win._anim_driver.anim_to_price, 0.0)

    def test_new_press_clears_stale_gesture(self):
        """S5：丢一次 mouseRelease 会让 _resizing 粘滞为真；下一次按下必须复位，
        否则在中部拖动窗口会被误当成拉伸高度。"""
        self.win._resizing = True
        self.win._press_chart = True
        center = QPointF(self.win.width() / 2.0, self.win.height() / 2.0)
        evt = QMouseEvent(
            QMouseEvent.Type.MouseButtonPress, center, center,
            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier)
        self.win.mousePressEvent(evt)
        self.assertFalse(self.win._resizing,
                         "新的左键按下必须清掉上一段丢失的拉伸手势")


class TestSecidBareCode(unittest.TestCase):
    """_secid 裸数字代码必须走 market_prefix（唯一实现），否则沪市 ETF/转债被
    误配成深市东财 secid，主源故障时备源分时恒空。"""

    def _sid(self, code):
        return sf_quote._secid(code)

    def test_sh_etf_and_convertible_bond(self):
        self.assertEqual(self._sid("510300"), "1.510300")   # 沪深300ETF（沪市基金）
        self.assertEqual(self._sid("588000"), "1.588000")   # 科创50ETF（沪市）
        self.assertEqual(self._sid("110060"), "1.110060")   # 沪市可转债

    def test_sz_instruments_unchanged(self):
        self.assertEqual(self._sid("159915"), "0.159915")   # 深市 ETF
        self.assertEqual(self._sid("600519"), "1.600519")   # 沪市主板
        self.assertEqual(self._sid("000001"), "0.000001")   # 深市主板

    def test_bj_pads_and_markets(self):
        self.assertEqual(self._sid("8307"), "0.830700")     # 北交所 4 位老代码补零
        self.assertEqual(self._sid("920001"), "0.920001")   # 920 段仍归东财 0 段

    def test_prefixed_codes_unchanged(self):
        self.assertEqual(self._sid("sh600519"), "1.600519")
        self.assertEqual(self._sid("sz000001"), "0.000001")


class TestMarketPrefixFixedIncome(unittest.TestCase):
    """建议#2：沪市固收段（204 国债回购 / 132 可交换债）不得被泛规则判成深市。"""

    def test_shanghai_fixed_income_is_sh(self):
        self.assertEqual(sf_utils.market_prefix("204001"), "sh")   # GC001
        self.assertEqual(sf_utils.market_prefix("132001"), "sh")   # 可交换债

    def test_existing_segments_preserved(self):
        self.assertEqual(sf_utils.market_prefix("200001"), "sz")   # 深B
        self.assertEqual(sf_utils.market_prefix("510300"), "sh")   # 沪ETF
        self.assertEqual(sf_utils.market_prefix("110060"), "sh")   # 沪转债
        self.assertEqual(sf_utils.market_prefix("920001"), "bj")   # 北交所


class TestReleaseStageLabel(unittest.TestCase):
    """单 feed 的 Beta/稳定可见标签：只影响文案与清单附加字段，不改变更新链路。"""

    def _signed_manifest(self, **changes):
        data = {"schema": 1, "channel": "stable", "version": "1.9.9",
                "bundle_id": sf_constants.BUNDLE_ID,
                "app_name": sf_constants.APP_DISPLAY_NAME + ".app", "notes": "x",
                "archive": {"url": "https://github.com/a/b/releases/download/v/a.zip",
                            "size": 3, "sha256": hashlib.sha256(b"zip").hexdigest()}}
        data.update(changes)
        payload = sf_update._canonical_payload(data)
        key = SigningKey.generate()
        data["signature"] = {"algorithm": "ed25519",
                             "value": base64.b64encode(key.sign(payload).signature).decode("ascii")}
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        return raw, key

    def _verify(self, **changes):
        raw, key = self._signed_manifest(**changes)
        with patch.object(sf_update, "UPDATE_PUBLIC_KEY_B64",
                          base64.b64encode(key.verify_key.encode()).decode()):
            return sf_update.verify_manifest(raw)

    def test_stage_parsed_when_present(self):
        self.assertEqual(self._verify(stage="beta").stage, "beta")
        self.assertEqual(self._verify(stage="stable").stage, "stable")

    def test_stage_defaults_stable_when_absent(self):
        # 旧清单不含 stage → 必须按 stable，保证照常更新的措辞不变。
        self.assertEqual(self._verify().stage, "stable")

    def test_bogus_stage_falls_back_stable(self):
        # 伪造/异常 stage 值不得影响"照常更新"，一律退回 stable 展示。
        self.assertEqual(self._verify(stage="nightly").stage, "stable")

    def test_offer_headline_marks_beta(self):
        self.assertIn("Beta", sf_update.offer_headline("1.9.9", "beta"))
        self.assertNotIn("Beta", sf_update.offer_headline("1.9.9", "stable"))

    def test_version_label_derivation(self):
        # 不打断言具体版本号；只校验 label 与 stage 的派生一致性。
        beta = sf_constants.RELEASE_STAGE == "beta"
        self.assertEqual(sf_constants.VERSION_LABEL.endswith(" Beta"), beta)


if __name__ == "__main__":
    unittest.main()
