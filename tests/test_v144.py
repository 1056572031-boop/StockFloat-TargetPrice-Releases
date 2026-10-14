# -*- coding: utf-8 -*-
"""实时股价 v1.2.44 未开盘分时图展示回归测试。

需求：未开盘（盘前/收盘后/休市/周末）期间，悬浮窗仍正常展示最近交易日分时走势图。

根因（v1.2.44 修复）：
    sf_quote._fetch_minute_primary / fetch_minute_fallback 原先统一按
    min(cur_min, close_min) 裁剪分时点上界。非交易时段 cur_only=False 时应接受
    最近交易日完整分时（9:30~15:00），但盘前/上午休市/周末上午 cur_min 早于
    15:00，导致 9:30 之后的分时点被全量过滤成空 → 未开盘期间分时图空白。
    修复：非交易时段固定 close_min（15:00）为上界，完整保留最近交易日走势；
    交易时段维持 min(cur_min, close_min) 裁剪，行为不回归。

运行方式（在 StockFloat 目录）：
    python3 -m unittest tests/test_v144.py -v
"""
import datetime
import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sf_quote
from sf_constants import APP_VERSION

# 测试用固定时钟：代理真实 datetime，但 now()/today() 固定，便于模拟时段。
# 用两层嵌套类：datetime/date 是嵌套类，now()/today() 是 staticmethod，类访问正确打桩。
_FIXED = {}


class _FakeDatetime:
    """注入固定时刻的 datetime 替身。

    v1.2.72：被测代码（分时主/备源）统一改用 _beijing_now() 取时刻（固定
    UTC+8，不再依赖本机时区），替身因此需提供 `timezone` 与带 tz 参数的
    now()。tz 参数在此被忽略、直接返回注入的固定时刻 —— 用例里的固定时刻
    本就按**北京时间**编写（如 10:00 表示北京 10:00），与生产语义一致。
    """
    timezone = datetime.timezone
    timedelta = datetime.timedelta

    class datetime:
        @staticmethod
        def now(tz=None):
            return _FIXED["now"]

        @staticmethod
        def utcnow():
            return _FIXED["now"]

        # 保留构造能力，供被测代码构造 datetime 对象（当前未用到，防御性补齐）
        def __init__(self, *a, **k):
            self._d = datetime.datetime(*a, **k)

    class date:
        @staticmethod
        def today():
            return _FIXED["now"].date()

        def __init__(self, *a, **k):
            self._d = datetime.date(*a, **k)


# 腾讯主源分时：2026-08-27（昨日/最近交易日）9:30~15:00 完整 6 个点位
_ROWS_LAST_DAY = [
    "0930 1510.0 100 151000",
    "1000 1512.0 500 757000",
    "1130 1520.0 900 1369000",
    "1300 1522.0 1200 1828000",
    "1430 1530.0 1500 2295000",
    "1500 1535.0 2000 3068000",
]
_ROWS_TODAY = [
    "0930 1510.0 100 151000",
    "1000 1512.0 500 757000",
    "1130 1520.0 900 1369000",
    "1300 1522.0 1200 1828000",
    "1430 1530.0 1500 2295000",
    "1500 1535.0 2000 3068000",
]


def _tencent_json(code, date_str, rows):
    return json.dumps(
        {"code": 0, "msg": "", "data": {
            code: {"data": {"date": date_str, "data": rows}}}}, ensure_ascii=False)


# 东财备用源 trends：时间,开,收,高,低,量,额,均价
_TRENDS_LAST_DAY = [
    "2026-08-27 09:30,1490,1510.0,1512,1488,12345,151000000,1509.5",
    "2026-08-27 10:00,1510,1512.0,1515,1505,45678,567800000,1511.2",
    "2026-08-27 11:30,1518,1520.0,1522,1516,67890,846700000,1519.0",
    "2026-08-27 13:00,1520,1522.0,1525,1519,78901,983200000,1521.3",
    "2026-08-27 14:30,1528,1530.0,1532,1526,90012,1123400000,1529.1",
    "2026-08-27 15:00,1533,1535.0,1537,1531,101234,1267000000,1534.0",
]
_TRENDS_TODAY = [
    "2026-08-28 09:30,1490,1510.0,1512,1488,12345,151000000,1509.5",
    "2026-08-28 10:00,1510,1512.0,1515,1505,45678,567800000,1511.2",
    "2026-08-28 11:30,1518,1520.0,1522,1516,67890,846700000,1519.0",
    "2026-08-28 13:00,1520,1522.0,1525,1519,78901,983200000,1521.3",
    "2026-08-28 14:30,1528,1530.0,1532,1526,90012,1123400000,1529.1",
    "2026-08-28 15:00,1533,1535.0,1537,1531,101234,1267000000,1534.0",
]


def _eastmoney_json(trends):
    return json.dumps({"data": {"trends": trends}}, ensure_ascii=False)




class TestMinuteOffMarketPrimary(unittest.TestCase):
    """主源（腾讯）非交易时段：盘前/周末上午必须完整保留最近交易日分时。"""

    def tearDown(self):
        sf_quote.http_get = None  # 占位防残留（真实还原由 patch 负责）

    def test_premarket_keeps_full_last_trading_day(self):
        """盘前 08:00（周五）：date=昨日 → cur_only=False，9:30~15:00 全保留。"""
        _FIXED["now"] = datetime.datetime(2026, 8, 28, 8, 0, 0)  # 周五盘前
        code = "sh600519"
        with patch.object(sf_quote, "datetime", _FakeDatetime), \
                patch.object(sf_quote, "http_get",
                             return_value=_tencent_json(code, "20260827", _ROWS_LAST_DAY)):
            pts = sf_quote._fetch_minute_primary(code)
        times = [p[0] for p in pts]
        self.assertEqual(
            times, ["0930", "1000", "1130", "1300", "1430", "1500"],
            "盘前应展示最近交易日完整分时，实际: %r" % (times,))

    def test_weekend_morning_keeps_full_last_trading_day(self):
        """周末上午 10:00（休市）：同样完整保留最近交易日分时（上线不再空白）。"""
        _FIXED["now"] = datetime.datetime(2026, 8, 29, 10, 0, 0)  # 周六
        code = "sh600519"
        with patch.object(sf_quote, "datetime", _FakeDatetime), \
                patch.object(sf_quote, "http_get",
                             return_value=_tencent_json(code, "20260828", _ROWS_LAST_DAY)):
            pts = sf_quote._fetch_minute_primary(code)
        self.assertEqual(len(pts), 6, "周末应保留完整最近交易日分时")

    def test_evening_after_close_keeps_full_day(self):
        """收盘后 18:00：cur_only=False（date=昨日），完整保留。"""
        _FIXED["now"] = datetime.datetime(2026, 8, 28, 18, 0, 0)
        code = "sh600519"
        with patch.object(sf_quote, "datetime", _FakeDatetime), \
                patch.object(sf_quote, "http_get",
                             return_value=_tencent_json(code, "20260827", _ROWS_LAST_DAY)):
            pts = sf_quote._fetch_minute_primary(code)
        self.assertEqual(len(pts), 6, "收盘后应保留最近交易日完整分时")


class TestMinuteTradingPrimary(unittest.TestCase):
    """主源（腾讯）交易时段：仍按 9:30~当前时刻裁剪，行为不回归。"""

    def test_intraday_trims_to_current_time(self):
        """交易中 10:00（date=今日）→ cur_only=True，仅保留 ≤10:00 的点。"""
        _FIXED["now"] = datetime.datetime(2026, 8, 28, 10, 0, 0)  # 周五 10:00
        code = "sh600519"
        with patch.object(sf_quote, "datetime", _FakeDatetime), \
                patch.object(sf_quote, "http_get",
                             return_value=_tencent_json(code, "20260828", _ROWS_TODAY)):
            pts = sf_quote._fetch_minute_primary(code)
        times = [p[0] for p in pts]
        self.assertEqual(times, ["0930", "1000"],
                         "交易时段应裁剪到当前时刻，实际: %r" % (times,))

    def test_postclose_today_drops_afterhours_rows(self):
        """收盘后 date=今日（cur_only=True）→ 上限 15:00，盘后点不残留。"""
        _FIXED["now"] = datetime.datetime(2026, 8, 28, 16, 0, 0)
        rows = _ROWS_TODAY + ["1510 1540.0 2100 3220000"]  # 模拟腾讯盘后分时点
        code = "sh600519"
        with patch.object(sf_quote, "datetime", _FakeDatetime), \
                patch.object(sf_quote, "http_get",
                             return_value=_tencent_json(code, "20260828", rows)):
            pts = sf_quote._fetch_minute_primary(code)
        times = [p[0] for p in pts]
        self.assertNotIn("1510", times, "盘后点（15:00 后）不应保留")

    def test_trading_but_stale_date_dropped(self):
        """交易时段却返回非当日数据 → 丢弃（防昨日拼接画错进度）。"""
        _FIXED["now"] = datetime.datetime(2026, 8, 28, 10, 0, 0)
        code = "sh600519"
        with patch.object(sf_quote, "datetime", _FakeDatetime), \
                patch.object(sf_quote, "http_get",
                             return_value=_tencent_json(code, "20260827", _ROWS_LAST_DAY)):
            pts = sf_quote._fetch_minute_primary(code)
        self.assertEqual(pts, [], "交易时段拿到非当日数据必须丢弃")


class TestMinuteFallback(unittest.TestCase):
    """备用源（东财 trends2）：非交易时段完整保留 / 交易时段裁剪。"""

    def test_off_market_keeps_full(self):
        _FIXED["now"] = datetime.datetime(2026, 8, 28, 8, 0, 0)  # 盘前
        code = "600519"
        with patch.object(sf_quote, "datetime", _FakeDatetime), \
                patch.object(sf_quote, "http_get",
                             return_value=_eastmoney_json(_TRENDS_LAST_DAY)):
            pts = sf_quote.fetch_minute_fallback(code)
        times = [p[0] for p in pts]
        self.assertEqual(
            times, ["09:30", "10:00", "11:30", "13:00", "14:30", "15:00"],
            "备用源盘前也应保留最近交易日完整分时，实际: %r" % (times,))

    def test_intraday_trims_fallback(self):
        _FIXED["now"] = datetime.datetime(2026, 8, 28, 10, 0, 0)
        code = "600519"
        with patch.object(sf_quote, "datetime", _FakeDatetime), \
                patch.object(sf_quote, "http_get",
                             return_value=_eastmoney_json(_TRENDS_TODAY)):
            pts = sf_quote.fetch_minute_fallback(code)
        times = [p[0] for p in pts]
        self.assertEqual(times, ["09:30", "10:00"],
                         "备用源交易时段应裁剪到当前时刻，实际: %r" % (times,))


if __name__ == "__main__":
    unittest.main()
