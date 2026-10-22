# -*- coding: utf-8 -*-
"""铃铛气泡和主窗的接线：悬停时百分比随现价走、触发后行挪组、清空自动收起、
气泡没开时行情更新不会把它顶出来、刷新失败不拖垮行情主链路。

用真 FloatWindow 加完整隔离桩（见 AGENTS §6 第 14 条：被测窗口只能由带隔离桩的夹具来建）。
内部状态一律走 _wire / _peek 两个入口（§6 第 12 条），不在判据里散写 `win._x`。
"""
from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from PyQt6.QtCore import QPoint
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

import sf_quote
import stock_float_window as m
from tests._base import SFCase


def _fake_http_get(url, **kw):
    if "hq.sinajs.cn" in url:
        return 'var hq_str_sh600519="贵州茅台,1500.0,1490.0,1510.0,1520.0,1480.0";'
    if "/appstock/app/minute/query?" in url:
        return ('{"data":{"sh600519":{"data":{"date":"20260821",'
                '"data":["0930 1510.0 100 1000", "1000 1511.0 200 2000"]}}}}')
    return 'v_sh600519="1~贵州茅台~600519~1510.0~1490.0~0.1~1.01~10.5~9.8";'


def _wire(host, **members):
    """把内部状态装到宿主上（成员名写在关键字参数里，集中一个入口）。"""
    for name, value in members.items():
        setattr(host, name, value)


def _peek(host, *names):
    values = [getattr(host, name) for name in names]
    return values[0] if len(values) == 1 else tuple(values)


def _alert(direction, target, state="pending"):
    return {"direction": direction, "target": target, "state": state,
            "session_date": "2099-12-31"}


_QAPP = None


class LiveBubbleCase(SFCase):
    def setUp(self):
        global _QAPP
        super().setUp()
        # 必须自己持有 QApplication 引用，否则返回值一丢就被回收（见 test_target_bubble_text 同款写法）
        _QAPP = SFCase.ensure_qapp()
        for p in (
            patch.object(m, "load_config", return_value=("sh600519", 100, 100, 200, "translucent")),
            patch.object(m, "save_config", lambda *a, **k: True),
            patch.object(sf_quote, "http_get", _fake_http_get),
            patch.object(m, "start_icon_worker", lambda: None),
            patch.object(m.FloatWindow, "_force_top", lambda self: None),
            patch.object(m.FloatWindow, "_setup_reopen_handler", lambda self: None),
            patch("PyQt6.QtWidgets.QMenu.popup", lambda *a, **k: None),
        ):
            self.track_patch(p)
        self.win = m.FloatWindow()
        self.win.running = False
        _peek(self.win, "_wake").set()
        self.win.timer.stop()
        self.win.top_timer.stop()
        self.win.show()
        QApplication.processEvents()
        _wire(self.win, _price_is_fallback=False, _real_price=60.0, _alerts=[])

    def tearDown(self):
        # 关窗必须发生在 patch 还活着的时候（§6 第 11 条），否则退出落盘会写用户真实配置
        _wire(self.win, _exit_persisted=True)
        self.win.close()
        QApplication.processEvents()
        super().tearDown()

    # ---- 辅助 ----
    def bubble(self):
        return _peek(self.win, "_target_bubble")

    def hover_bell(self):
        # 全局鼠标位置在用例之间是保留的：上一个用例已经停在铃铛上时，再移到同一点会被当作
        # 「没动」而不送出移动事件（单跑全过、一起跑成簇红的根因）。所以每次都先挪到窗口里
        # 一个不命中任何按钮的位置，保证到铃铛这一下位置一定变化。
        QTest.mouseMove(self.win, QPoint(20, 50))
        QApplication.processEvents()
        QTest.mouseMove(self.win, QPoint(self.win.width() - m.BTN_X["bell"], m.BTN_CY))
        QApplication.processEvents()

    def refresh(self):
        _peek(self.win, "_refresh_target_bubble")()

    def open_with(self, alerts):
        _wire(self.win, _alerts=alerts)
        self.hover_bell()
        self.assertTrue(self.bubble().isVisible(), "悬停到铃铛上应弹出气泡")

    # ---- 用例 ----
    def test_悬停打开气泡_百分比取当前判定价(self):
        self.open_with([_alert("above", 63.0)])
        self.assertEqual(self.bubble().rows()["pending"], [(True, "63.00", "+5.0%")])

    def test_气泡开着时现价变了_百分比跟着变_位置不动(self):
        self.open_with([_alert("above", 63.0)])
        before = self.bubble().body_rect_global()
        _wire(self.win, _real_price=61.0)
        self.refresh()
        self.assertEqual(self.bubble().rows()["pending"], [(True, "63.00", "+3.3%")])
        self.assertEqual(self.bubble().body_rect_global().left(), before.left(),
                         "只是数字变了，气泡不该横向跳动")
        self.assertTrue(self.bubble().isVisible())

    def test_内容没变时刷新不重新摆放也不重新前置(self):
        self.open_with([_alert("above", 63.0)])
        shown = Mock()
        _wire(self.bubble(), show_below=shown)
        self.refresh()
        shown.assert_not_called()
        _wire(self.win, _real_price=61.0)
        self.refresh()
        shown.assert_called_once()

    def test_触发后这一行从待触发挪到已触发_气泡不消失(self):
        alerts = [_alert("above", 63.0), _alert("below", 57.0)]
        self.open_with(alerts)
        alerts[0]["state"] = "hit"
        self.refresh()
        rows = self.bubble().rows()
        self.assertEqual([r[1] for r in rows["pending"]], ["57.00"])
        self.assertEqual(rows["hit"], [(True, "63.00", "")])
        self.assertTrue(self.bubble().isVisible())

    def test_目标全部清空后气泡自己收起(self):
        alerts = [_alert("above", 63.0)]
        self.open_with(alerts)
        alerts.clear()
        self.refresh()
        self.assertFalse(self.bubble().isVisible())

    def test_气泡没开或鼠标不在铃铛上时_刷新不会把它顶出来(self):
        _wire(self.win, _alerts=[_alert("above", 63.0)])
        self.refresh()
        self.assertFalse(self.bubble().isVisible(), "没悬停过，刷新不该弹气泡")
        self.hover_bell()
        self.assertTrue(self.bubble().isVisible())
        self.bubble().hide()
        self.refresh()
        self.assertFalse(self.bubble().isVisible(), "气泡被收起后，行情更新不能把它重新顶出来")
        self.hover_bell()
        _wire(self.win, _hover_btn=None)
        self.bubble().show()
        self.refresh()
        self.assertTrue(self.bubble().isVisible(), "鼠标不在铃铛上时刷新什么都不做，也不替用户收起")

    def test_行情更新会触发一次气泡刷新(self):
        hook = Mock()
        _wire(self.win, _refresh_target_bubble=hook)
        data = {"price": "61.00", "pct": "1.67", "high": "62", "low": "60",
                "code": "600519", "change": "1.00", "pre_close": "60.00"}
        _peek(self.win, "_apply_quote")(data)
        hook.assert_called_once_with()

    def test_气泡刷新抛错只留一行痕迹_行情更新照常完成(self):
        _wire(self.win, _refresh_target_bubble=Mock(side_effect=RuntimeError("boom")))
        data = {"price": "61.00", "pct": "1.67", "high": "62", "low": "60",
                "code": "600519", "change": "1.00", "pre_close": "60.00"}
        with patch.object(m, "log_swallow") as swallowed:
            _peek(self.win, "_apply_quote")(data)
        labels = [c.args[0] for c in swallowed.call_args_list]
        self.assertIn("stock_float_window._apply_quote.bubble_refresh", labels)
        self.assertIsNotNone(_peek(self.win, "quote"), "气泡出错不能挡住后面的行情提交")


if __name__ == "__main__":
    unittest.main(verbosity=2)
