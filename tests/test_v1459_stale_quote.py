# -*- coding: utf-8 -*-
"""v1.4.59（S3）：行情坏了不许冒充实时；长期跑备源这件事必须可查。

两条都是"界面看起来一切正常，数据其实不可用"的形状：
  ① `QuoteDataError`（接口返回坏数据）分支只降频、从不 `_report_net(False)`，
     而"网络异常 HH:MM:SS"只在 `_net_ok=False` 才绘制 → 价格冻在旧值、
     呼吸灯照旧闪，用户拿它当实时价做决定。本项目的降级原则是
     "保留 + 标滞、不白屏、**不冒充实时**"。
  ② 主源格式永久变更而新浪备源正常时，备源成功会复位格式错误计数
     （v1.2.69 为防刷屏做的，本身没错），于是 `QUOTE FORMAT ERROR x3`
     这道告警永远不响 —— 可以几个月都跑在备源上而没人知道。
     备源没有 volume/limit_up/limit_down → 涨跌停徽标与
     "目标价不得越过涨跌停"的拦截同时静默失效。
"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtWidgets import QApplication                       # noqa: E402

import sf_quote                                                 # noqa: E402
import stock_float_window as m                                  # noqa: E402
from sf_network import QuoteDataError                           # noqa: E402

try:
    from _base import SFCase
except ImportError:                                             # pragma: no cover
    from tests._base import SFCase


class BadDataMarksThePriceStale(SFCase):
    """①坏数据也要让界面标"不新鲜"。

    用轻量宿主而不是真窗口：`_fetch_quote` 开头是"防重入"闸门，真窗口的后台
    行情循环会抢这把锁，用例就会时快时慢地走进 early return（表现是
    "单独跑绿、全量红"那一类不可复现失败）。宿主上绑的是产品真方法。
    """

    class _Host:
        _fetch_quote = m.FloatWindow._fetch_quote   # discipline-ok(R5): 宿主按名字绑产品真方法，这条路径没有公开入口

        def __init__(self):
            import threading
            self.code = "sh600519"
            self._quote_lock = threading.Lock()
            self._quote_fail = 0
            self.net_reports = []
            self.results = []

        def _q_put(self, item):
            pass

        def _on_poll_result(self, ok, **kw):
            self.results.append(ok)

        def _report_net(self, ok):
            self.net_reports.append(ok)

    def test_repeated_bad_data_reports_not_live(self):
        host = self._Host()
        with patch.object(m, "fetch_quote",
                          side_effect=QuoteDataError("malformed payload: ''")):
            for _ in range(m.NET_FAIL_SHOW):
                host._fetch_quote()   # discipline-ok(R5): 同上：调的是宿主上绑的产品真方法
        self.assertIn(False, host.net_reports,
                      "连续 %d 次坏数据之后界面仍显示成实时 —— S3 第一处"
                      % m.NET_FAIL_SHOW)
        self.assertEqual(host.results, [False] * m.NET_FAIL_SHOW,
                         "降频（熔断计数）这条原有语义不许被改动")

    def test_single_bad_packet_does_not_yet_flag_the_ui(self):
        """阈值仍然有效：一次抖动不该把界面打成"网络异常"。"""
        host = self._Host()
        with patch.object(m, "fetch_quote", side_effect=QuoteDataError("boom")):
            host._fetch_quote()   # discipline-ok(R5): 同上：调的是宿主上绑的产品真方法
        self.assertEqual(host.net_reports, [], "一次坏包就上界面 = 抖动放大成告警")

    def test_good_data_clears_the_flag(self):
        """恢复真数据之后必须把"不新鲜"撤掉，否则这条标记会永久挂着。"""
        host = self._Host()
        with patch.object(m, "fetch_quote", side_effect=QuoteDataError("boom")):
            for _ in range(m.NET_FAIL_SHOW):
                host._fetch_quote()   # discipline-ok(R5): 同上：调的是宿主上绑的产品真方法
        with patch.object(m, "fetch_quote", return_value={"price": 10.0}), \
             patch.object(m, "get_last_quote_source", lambda: "tencent"):
            host._fetch_quote()   # discipline-ok(R5): 同上：调的是宿主上绑的产品真方法
        self.assertEqual(host.net_reports[-1], True, "恢复后必须撤掉那条标记")


class DegradedSourceIsCountable(SFCase):
    """②主源坏、备源撑着：单独记账，且只在主源活着时归零。"""

    def setUp(self):
        super().setUp()
        self._reset = sf_quote.degraded_source_streak()
        sf_quote._degraded_streak = 0   # discipline-ok(R5): offscreen 下没有公开入口能构造这个现场
        self.addCleanup(setattr, sf_quote, "_degraded_streak", self._reset)

    def _primary_broken_backup_ok(self):
        with patch.object(sf_quote, "_fetch_quote_primary",
                          side_effect=QuoteDataError("primary format changed")), \
             patch.object(sf_quote, "fetch_quote_fallback",
                          return_value={"price": 10.0}), \
             patch.object(sf_quote, "log_line"):
            sf_quote.fetch_quote("sh600519")

    def test_backup_success_counts_as_degraded(self):
        self._primary_broken_backup_ok()
        self.assertEqual(sf_quote.degraded_source_streak(), 1,
                         "备源兜住 ≠ 主源健康；这件事必须单独记得")

    def test_streak_survives_the_format_counter_reset(self):
        """v1.2.69 的复位继续保留（别把"有数据"报成"接口挂了"），但降级账不清。"""
        self._primary_broken_backup_ok()
        self.assertEqual(sf_quote._format_err_count["quote"], 0,   # discipline-ok(R5): offscreen 下没有公开入口能构造这个现场
                         "格式错误计数仍应被备源成功复位（旧意图不变）")
        self.assertEqual(sf_quote.degraded_source_streak(), 1)

    def test_primary_success_resets_the_streak(self):
        self._primary_broken_backup_ok()
        with patch.object(sf_quote, "_fetch_quote_primary",
                          return_value={"price": 11.0}):
            sf_quote.fetch_quote("sh600519")
        self.assertEqual(sf_quote.degraded_source_streak(), 0,
                         "主源活了就必须归零，否则这条数字只会单调涨")

    def test_first_degraded_hit_names_the_consequence(self):
        msgs = []
        with patch.object(sf_quote, "_fetch_quote_primary",
                          side_effect=QuoteDataError("primary format changed")), \
             patch.object(sf_quote, "fetch_quote_fallback",
                          return_value={"price": 10.0}), \
             patch.object(sf_quote, "log_line",
                          lambda msg, level="INFO": msgs.append(msg)):
            sf_quote.fetch_quote("sh600519")
        joined = " ".join(msgs)
        self.assertIn("跑备源", joined)
        self.assertIn("涨跌停", joined,
                      "必须说清失效的是什么，否则这条日志等于没说")


if __name__ == "__main__":
    unittest.main()
