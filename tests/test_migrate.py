# -*- coding: utf-8 -*-
"""实时股价 v1.1.0 配置迁移与数据源容错单元测试（16.2 / 17.2 补充）。

覆盖：
- _migrate_legacy_config：旧路径配置迁移到规范目录（成功/已存在跳过/坏 JSON 保留旧文件）
- fetch_quote / fetch_minute 备用源切换与格式错误告警（4.1 / 4.2）

运行方式（在 StockFloat 目录）：
    python3 -m unittest discover -s tests -v
"""
import os
import sys
import json
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import stock_float_window as m
import sf_config
import sf_constants
import sf_quote
from tests._base import fixed_clock

# 分时点只在"不晚于当前分钟"时才保留（sf_quote.fetch_minute），所以用例必须固定"现在几点"：
# 用真实时钟的话，交易日 09:15-09:29 之间两个点（09:30 与 10:30）全被截掉，用例必红。
# 取一个普通交易日的午后，两个点都在当前分钟之前。
_AFTERNOON = (2026, 10, 14, 14, 0)

_ORIG_CONFIG = m.CONFIG_PATH
_ORIG_LEGACY = sf_constants.LEGACY_CONFIG_PATH
_ORIG_CONFIG_SFC = sf_config.CONFIG_PATH
_ORIG_LEGACY_SFC = sf_config.LEGACY_CONFIG_PATH


class TestMigrateLegacyConfig(unittest.TestCase):
    """17.2：_migrate_legacy_config 迁移逻辑"""

    def setUp(self):
        self.td = tempfile.mkdtemp()
        sf_config.CONFIG_PATH = os.path.join(self.td, "cfg", "config.json")
        sf_config.SPLIT_SOURCE_CONFIG_PATH = os.path.join(self.td, "formal.json")
        sf_config.LEGACY_CONFIG_PATH = os.path.join(self.td, "legacy.json")

    def tearDown(self):
        sf_config.CONFIG_PATH = _ORIG_CONFIG_SFC
        sf_config.SPLIT_SOURCE_CONFIG_PATH = os.path.expanduser(
            "~/Library/Application Support/StockFloat/config.json")
        sf_config.LEGACY_CONFIG_PATH = _ORIG_LEGACY_SFC

    @staticmethod
    def _write(path, data):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)

    def test_formal_split_source_has_priority_and_is_preserved(self):
        formal = {"code": "sh600000", "x": 1, "y": 2, "h": 200, "bg": "glass"}
        hidden = {"code": "sz000001"}
        self._write(sf_config.SPLIT_SOURCE_CONFIG_PATH, formal)
        self._write(sf_config.LEGACY_CONFIG_PATH, hidden)
        m._migrate_legacy_config()
        with open(sf_config.CONFIG_PATH, encoding="utf-8") as f:
            self.assertEqual(json.load(f), formal)
        self.assertTrue(os.path.exists(sf_config.SPLIT_SOURCE_CONFIG_PATH),
                        "拆分前正式配置只能复制，绝不删除")
        self.assertTrue(os.path.exists(sf_config.LEGACY_CONFIG_PATH),
                        "正式路径优先时不得改动旧隐藏配置")

    def test_hidden_legacy_is_fallback_and_is_preserved(self):
        data = {"code": "sh600000", "x": 1, "y": 2, "h": 200, "bg": "glass"}
        self._write(sf_config.LEGACY_CONFIG_PATH, data)
        m._migrate_legacy_config()
        self.assertTrue(os.path.exists(sf_config.CONFIG_PATH))
        self.assertTrue(os.path.exists(sf_config.LEGACY_CONFIG_PATH),
                        "旧隐藏路径也只能复制，绝不删除")
        with open(sf_config.CONFIG_PATH, encoding="utf-8") as f:
            self.assertEqual(json.load(f), data)

    def test_skips_when_new_config_exists(self):
        new_data = {"code": "sz000001", "x": 10, "y": 20, "h": 210}
        os.makedirs(os.path.dirname(sf_config.CONFIG_PATH), exist_ok=True)
        with open(sf_config.CONFIG_PATH, "w", encoding="utf-8") as f:
            __import__("json").dump(new_data, f)
        with open(sf_config.LEGACY_CONFIG_PATH, "w", encoding="utf-8") as f:
            __import__("json").dump({"code": "sh600000"}, f)
        m._migrate_legacy_config()
        with open(sf_config.CONFIG_PATH, encoding="utf-8") as f:
            self.assertEqual(__import__("json").load(f), new_data)
        # 新配置已存在时不迁移，旧文件保留
        self.assertTrue(os.path.exists(sf_config.LEGACY_CONFIG_PATH))

    def test_keeps_legacy_on_bad_json(self):
        with open(sf_config.LEGACY_CONFIG_PATH, "w", encoding="utf-8") as f:
            f.write("{not valid json")
        m._migrate_legacy_config()
        self.assertFalse(os.path.exists(sf_config.CONFIG_PATH))
        self.assertTrue(os.path.exists(sf_config.LEGACY_CONFIG_PATH))


class TestQuoteFallback(unittest.TestCase):
    """4.1 / 4.2：主源异常自动切备用源；格式错误连续告警不改变正常流程"""

    def test_quote_fallback_on_primary_error(self):
        # 主源返回坏数据 -> 自动切新浪备用源成功
        primary_called = []

        def fake_get(url, **kw):
            primary_called.append(url)
            if "qt.gtimg.cn" in url:
                return "garbage-no-equals"
            # 新浪备用：名称,今开,昨收,现价,最高,最低
            return 'var hq_str_sh600519="贵州茅台,1500.0,1490.0,1510.0,1520.0,1480.0";'

        sf_quote.http_get = fake_get
        q = m.fetch_quote("sh600519")
        self.assertEqual(q["name"], "贵州茅台")
        # v1.2.65：价格小数位按品种归一（股票 2 位）。mock 里的 "1510.0" 是 1 位，
        # 真实新浪接口对股票返回 3 位（"1297.500"）、腾讯返回 2 位（"1297.50"），
        # 两者不一致正是归一要解决的问题 —— 归一后统一为 "1510.00"。
        self.assertEqual(q["price"], "1510.00")
        self.assertEqual(q["pre_close"], "1490.0")
        self.assertTrue(any("gtimg.cn" in u for u in primary_called))

    def test_quote_primary_ok_no_fallback(self):
        # 主源正常时不走备用源
        calls = []

        def fake_get(url, **kw):
            calls.append(url)
            return 'v_sh600519="1~贵州茅台~600519~1510.0~1490.0~1520.0";'

        sf_quote.http_get = fake_get
        q = m.fetch_quote("sh600519")
        self.assertEqual(q["name"], "贵州茅台")
        # v1.2.65：同上，价格小数位归一到股票 2 位
        self.assertEqual(q["price"], "1510.00")
        self.assertEqual(q["pre_close"], "1490.0")
        self.assertFalse(any("hq.sinajs.cn" in u for u in calls))

    def test_minute_fallback_on_primary_error(self):
        # 分时主源异常 -> 切东财备用源
        def fake_get(url, **kw):
            if "/appstock/app/minute/query?" in url:
                raise IOError("primary minute down")
            # 东财 trends2：trends 行形如 "时间,开,收,高,低,量,额,均价"
            return ('{"data":{"trends":['
                    '"09:30,1510.0,1510.0,1511.0,1509.0,100,100000.0,1510.0",'
                    '"10:30,1520.0,1520.0,1521.0,1519.0,200,200000.0,1520.0"]}}')

        sf_quote.http_get = fake_get
        with fixed_clock(_AFTERNOON):
            pts = m.fetch_minute("sh600519")
        self.assertTrue(pts)
        self.assertEqual(pts[0][1], 1510.0)

    def test_format_error_alert_threshold(self):
        # 连续 N 次格式错误达到阈值后计数复位（不改变正常流程）
        sf_quote._format_err_count["quote"] = sf_quote.FORMAT_ERROR_ALERT_THRESHOLD - 1

        def bad_get(url, **kw):
            return "no-equals"

        sf_quote.http_get = bad_get
        with self.assertRaises(m.QuoteDataError):
            m.fetch_quote("sh600519")
        # 触发告警后计数已复位
        self.assertEqual(sf_quote._format_err_count["quote"], 0)


class TestLoadAlertsDirtyEntries(unittest.TestCase):
    def test_dirty_entry_does_not_hide_valid_entries(self):
        path = os.path.join(tempfile.mkdtemp(), "config.json")
        valid = {"direction": "above", "target": 1600,
                 "session_date": "2026-12-31", "state": "pending"}
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"alerts": [None, valid, "broken", {"target": 1}]}, f)
        with patch.object(sf_config, "CONFIG_PATH", path):
            out = sf_config.load_alerts()
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["target"], 1600)

    @staticmethod
    def _load(entries):
        path = os.path.join(tempfile.mkdtemp(), "config.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"alerts": entries}, f)
        with patch.object(sf_config, "CONFIG_PATH", path):
            return sf_config.load_alerts()

    def test_numeric_string_target_is_normalized(self):
        """手工编辑配置写入的 "1600"（字符串）应归一成 float 而不是整条丢弃。"""
        out = self._load([{"direction": "above", "target": "1600",
                           "session_date": "2026-12-31"}])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["target"], 1600.0)
        self.assertIsInstance(out[0]["target"], float)

    def test_nan_target_is_rejected(self):
        """NaN 是 float 但不是合法价格：读进来会"看得见却永不生效"。"""
        out = self._load([
            {"direction": "above", "target": float("nan"),
             "session_date": "2026-12-31"},
            {"direction": "above", "target": 1600, "session_date": "2026-12-31"},
        ])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["target"], 1600)

    def test_non_positive_and_missing_session_date_rejected(self):
        out = self._load([
            {"direction": "above", "target": -1, "session_date": "2026-12-31"},
            {"direction": "above", "target": 0, "session_date": "2026-12-31"},
            {"direction": "above", "target": 1600},
            {"direction": "above", "target": 1600, "session_date": ""},
        ])
        self.assertEqual(out, [])

    def test_saves_when_directory_missing(self):
        """首启阶段配置目录尚未创建时，save_config 必须自建目录而不是抛异常。"""
        td = tempfile.mkdtemp()
        path = os.path.join(td, "nested", "dir", "config.json")
        orig = sf_config.CONFIG_PATH
        try:
            sf_config.CONFIG_PATH = path
            self.assertTrue(sf_config.save_config("sh600519", 100, 200))
            self.assertTrue(os.path.exists(path))
        finally:
            sf_config.CONFIG_PATH = orig


if __name__ == "__main__":
    unittest.main(verbosity=2)
