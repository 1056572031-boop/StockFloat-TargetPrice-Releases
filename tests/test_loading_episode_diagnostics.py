# -*- coding: utf-8 -*-
"""「加载中...」出现了几次、每次多久：诊断包里的答案要从真实绘制函数推上去。

用户反馈“股价变化不稳定，有时候显示加载中”，包里却只能看到此刻有没有数据，
回答不了“多久一次、一次多久”。这里用最小宿主直接驱动真实的 `_draw_chart` 与
`_paint_header`（宿主只装它们读到的成员，不起整个窗口，也就不会带出任何后台线程），
读数只走公开的 `paint_diagnostics()`；时钟用注入的假单调钟，不睡眠。
"""
import json
import pathlib
import sys
from types import SimpleNamespace
from unittest import mock

from PyQt6.QtCore import QRectF
from PyQt6.QtGui import QFont, QFontMetrics, QImage, QPainter

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import sf_ui_render  # noqa: E402
from sf_ui_render import RenderMixin  # noqa: E402

try:
    from _base import SFCase                  # noqa: E402
except ImportError:                           # 以包形式被 discover 导入时
    from tests._base import SFCase            # noqa: E402

CHART_RECT = QRectF(8, 120, 168, 90)
COLLAPSED_RECT = QRectF(8, 120, 168, 0)
CLOSED_MARKET = ("已收盘", None)
QUOTE = {"name": "测试", "code": "sh600000"}


class LoadingEpisodes(SFCase):
    def setUp(self):
        super().setUp()
        self.ensure_qapp()
        self.clock_value = 5000.0
        patcher = mock.patch("sf_ui_render.time.monotonic", side_effect=lambda: self.clock_value)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.image = QImage(184, 224, QImage.Format.Format_ARGB32)
        self.painter = QPainter(self.image)
        self.addCleanup(self.painter.end)
        self.status = None
        noop = lambda *args, **kwargs: None
        self.host = SimpleNamespace(
            quote=None, minute_points=[],
            _net_ok=True, _last_ok_time="", _minute_stale=False,
            _font=lambda *args, **kwargs: QFont(),
            _fm=lambda font: QFontMetrics(font),
            _market_status=lambda: self.status,
            _chart_metrics=lambda pts, rect: {},
            _draw_chart_base=noop, _draw_chart_avg=noop, _draw_chart_fill=noop,
            _draw_chart_line=noop, _draw_chart_dot=noop)
        self._settle()

    def _set(self, **members):
        vars(self.host).update(members)

    def _chart(self, rect=CHART_RECT):
        RenderMixin._draw_chart(self.host, self.painter, rect)  # discipline-ok(R5): 被测的真实绘制入口只有这个私有方法

    def _header(self):
        RenderMixin._paint_header(self.host, self.painter, 184)  # discipline-ok(R5): 同上

    def _loading(self):
        return sf_ui_render.paint_diagnostics()["loading"]

    def _settle(self):
        """先把前面用例（同进程里别的绘制）可能留下的进行中一段收掉，再取基线。"""
        self.status = CLOSED_MARKET
        self._set(quote=QUOTE, _net_ok=True, minute_points=[])
        self._chart()
        self._header()
        self.status = None
        self._set(quote=None)

    def test_chart_loading_is_counted_timed_and_closed(self):
        before = self._loading()["chart"]
        self._chart()
        self.clock_value += 2.0
        self._chart()
        self.clock_value += 3.0
        opened = self._loading()["chart"]
        self.assertEqual(opened["frames"] - before["frames"], 2)
        self.assertEqual(opened["open_since_monotonic"], 5000.0)
        self.assertAlmostEqual(opened["open_sec"], 5.0)
        self.status = CLOSED_MARKET
        self._chart()
        loading = self._loading()
        chart = loading["chart"]
        self.assertEqual(chart["episodes"] - before["episodes"], 1)
        self.assertIsNone(chart["open_since_monotonic"])
        self.assertIsNone(chart["open_sec"])
        self.assertGreaterEqual(chart["longest_closed_sec"], 5.0)
        last = loading["recent"][-1]
        self.assertEqual((last["kind"], last["start_monotonic"]), ("chart", 5000.0))
        self.assertAlmostEqual(last["duration_sec"], 5.0)

    def test_a_collapsed_chart_is_not_loading_time(self):
        before = self._loading()["chart"]
        self._chart()
        self.clock_value += 4.0
        self._chart(COLLAPSED_RECT)
        after = self._loading()["chart"]
        self.assertEqual(after["episodes"] - before["episodes"], 1)
        self.assertIsNone(after["open_since_monotonic"], "图区收起时用户看不到加载中，不能继续计时")
        self._chart(COLLAPSED_RECT)
        self.assertEqual(self._loading()["chart"]["episodes"], after["episodes"],
                         "没有进行中的段时再画收起帧，不许凭空多出一次")

    def test_real_points_end_the_loading_episode(self):
        before = self._loading()["chart"]
        self._chart()
        self.clock_value += 1.5
        self._set(minute_points=[(570, 10.0), (571, 10.1)])
        self._chart()
        after = self._loading()["chart"]
        self.assertEqual(after["episodes"] - before["episodes"], 1)
        self.assertIsNone(after["open_since_monotonic"])

    def test_header_loading_ends_on_network_error_and_does_not_reopen(self):
        before = self._loading()["header"]
        self._header()
        self.assertEqual(self._loading()["header"]["frames"] - before["frames"], 1)
        self.assertEqual(self._loading()["header"]["open_since_monotonic"], 5000.0)
        self.clock_value += 2.0
        self._set(_net_ok=False)
        self._header()
        mid = self._loading()["header"]
        self.assertEqual(mid["episodes"] - before["episodes"], 1, "断网显示的是“网络异常”，不是加载中")
        self.assertIsNone(mid["open_since_monotonic"])
        self._header()
        self.assertEqual(self._loading()["header"]["frames"], mid["frames"], "断网期间不再数加载中")
        self._set(_net_ok=True, quote=QUOTE)
        self._header()
        self.assertEqual(self._loading()["header"]["episodes"], mid["episodes"])

    def test_recent_list_is_bounded_and_the_export_is_plain_json(self):
        before = self._loading()["chart"]["episodes"]
        for _ in range(11):
            self.status = None
            self._chart()
            self.clock_value += 1.0
            self.status = CLOSED_MARKET
            self._chart()
        loading = self._loading()
        self.assertEqual(loading["chart"]["episodes"] - before, 11)
        self.assertLessEqual(len(loading["recent"]), 8, "最近记录必须有界，诊断包不能随运行时间变大")
        json.dumps(sf_ui_render.paint_diagnostics())   # 进诊断包前必须是纯 JSON 数据
