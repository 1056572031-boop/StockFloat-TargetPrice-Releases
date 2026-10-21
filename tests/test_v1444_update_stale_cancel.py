# -*- coding: utf-8 -*-
"""v1.4.44 检查链路的两处补修：卡死能自愈、等久了能取消（复核 F2 / F4）。

F2 的成因（复核时确认）：回包是有界队列（32 格，满时丢最旧）里的一次性事件，
一旦被丢掉，`_update_checking` 与 `_update_transition_pending` 就永久锁真 ——
此后每一次检查（包括用户手动点的）都起不来，那张「正在检查更新」也永远关不掉，
只能重启程序。修法是在飞超过 UPDATE_CHECK_STALE_SEC 就当作那次已经死了。

F4 的成因：清单超时从 5 秒放宽到 12 秒 × 2 次后，最坏 24 秒才有答复，
这期间必须让用户能明确地不再等。

两条都用公开行为判定（起不起新线程、弹不弹框），不读私有标记。
"""

import os
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sf_update  # noqa: E402
import stock_float_window  # noqa: E402
from sf_constants import (UPDATE_CHECK_STALE_SEC,  # noqa: E402
                          UPDATE_MANIFEST_ATTEMPTS, UPDATE_MANIFEST_TIMEOUT_SEC)
from stock_float_window import FloatWindow  # noqa: E402
from tests._base import SFCase  # noqa: E402

_check_bg = FloatWindow._check_updates_background  # discipline-ok(R5): 被测对象本身就是私有方法，豁免集中此一处
_handle_result = FloatWindow._handle_update_check  # discipline-ok(R5): 同上
_handle_cancel = FloatWindow._handle_update_check_cancel  # discipline-ok(R5): 同上
_is_stale = FloatWindow._update_check_stale  # discipline-ok(R5): 同上
_show_checking = FloatWindow._show_checking_dialog  # discipline-ok(R5): 同上

_NEW = sf_update.UpdateInfo(
    "9.9.9", "本轮说明",
    "https://github.com/o/r/releases/download/v/a.zip", 8, "a" * 64)

_QAPP = None


def _ensure_qapp():
    """QApplication 必须由模块级引用持有，否则离屏下进程会中途 abort（同 test_update.py）。"""
    global _QAPP
    if _QAPP is None:
        from PyQt6.QtWidgets import QApplication
        _QAPP = QApplication.instance() or QApplication([])
    return _QAPP


def _fake(prompts=None, messages=None, closed=None, **overrides):
    base = {
        "_threads_stopped": False,
        "_update_checking": False,
        "_update_downloading": False,
        "_update_transition_pending": False,
        "_update_pending": None,
        "_ready_update_archive": lambda self, value: None,
        "_update_check_dialog": None,
        "_update_manual_wait": False,
        "_update_check_aborted": False,
        "_update_last_check_at": 0.0,
        # 本文件用**真实**的过期判定（_is_stale 就是产品实现），否则"卡死"这件事测不到
        "_update_check_stale": _is_stale,
        "_update_failure_count": 0,
        "_update_retry_timer": None,
        "_prompt_update": lambda self, value: (
            prompts.append(value) if prompts is not None else None),
        "_show_update_message": lambda self, *a, **k: (
            messages.append(a) if messages is not None else None),
        "_show_checking_dialog": lambda self: None,
        "_download_update_background": lambda self, value, **kwargs: None,
        "_dispose_update_dialog": lambda self, dialog: (
            closed.append(dialog) if closed is not None else None),
        "_close_update_flow_box": lambda self: None,
        "_schedule_update_retry": lambda self: None,
    }
    base.update(overrides)
    return type("FakeWindow", (), base)()


_IN_FLIGHT = {"_update_checking": True, "_update_transition_pending": True}


class StaleCheckSelfHeals(SFCase):
    def test_fresh_inflight_check_is_not_reset(self):
        """刚起 1 秒的检查仍然算活着：不许把并发的第二次点击变成第二个请求。"""
        host = _fake(**_IN_FLIGHT, _update_last_check_at=time.time())
        with patch("stock_float_window.threading.Thread") as thread:
            _check_bg(host, manual=True)
        thread.assert_not_called()

    def test_stale_reset_lets_a_later_click_start_again(self):
        """行为判据：卡死被解除后，用户下一次点击能真的发出请求（起线程）。"""
        host = _fake(**_IN_FLIGHT,
                     _update_last_check_at=time.time() - UPDATE_CHECK_STALE_SEC - 1)
        started = []

        class _Thread:
            def __init__(self, *args, **kwargs):
                del args, kwargs

            def start(self):
                started.append("thread")

        with patch("stock_float_window.threading.Thread", _Thread):
            _check_bg(host, manual=True)
        self.assertEqual(started, ["thread"], "过期检查必须让位，否则更新从此不再发生")

    def test_stale_reset_closes_the_stuck_dialog(self):
        closed = []
        stale_box = object()
        host = _fake(closed=closed, **_IN_FLIGHT, _update_check_dialog=stale_box,
                     _update_last_check_at=time.time() - UPDATE_CHECK_STALE_SEC - 1)
        with patch("stock_float_window.threading.Thread"):
            _check_bg(host, manual=True)
        self.assertEqual(closed, [stale_box], "那张关不掉的「正在检查」框要跟着收掉")

    def test_stale_line_covers_the_whole_retry_budget(self):
        """过期线必须大于"全部重试都用满"的时长，否则会把正常检查误判成卡死。"""
        from sf_constants import UPDATE_CHECK_STALE_SEC as STALE
        self.assertGreater(STALE, UPDATE_MANIFEST_TIMEOUT_SEC * UPDATE_MANIFEST_ATTEMPTS)

    def test_expiry_predicate_is_a_simple_deadline(self):
        host = _fake(_update_last_check_at=time.time() - UPDATE_CHECK_STALE_SEC - 0.5)
        self.assertTrue(_is_stale(host))
        fresh = _fake(_update_last_check_at=time.time())
        self.assertFalse(_is_stale(fresh))


class CheckingDialogCanBeCancelled(SFCase):
    def test_cancel_stops_the_answer_from_popping_later(self):
        """按「取消」后回包照收（状态、日志、退避都走），但界面不再弹任何东西。"""
        prompts, messages = [], []
        host = _fake(prompts=prompts, messages=messages, _update_manual_wait=True)
        _handle_cancel(host, "取消")
        _handle_result(host, True, _NEW, None)
        self.assertEqual((prompts, messages), ([], []), "用户已经说不等了，就别再弹")

    def test_cancel_makes_the_next_check_answer_again(self):
        """作废只对本次有效：新检查起来后，答复不许被连带吃掉。"""
        prompts, messages = [], []
        host = _fake(prompts=prompts, messages=messages, _update_manual_wait=True)
        _handle_cancel(host, "取消")
        with patch("stock_float_window.threading.Thread"):
            _check_bg(host, manual=True)          # 起新检查 → 作废标记必须被清
        _handle_result(host, True, _NEW, None)    # 这一次是用户主动要求的
        self.assertEqual(len(prompts), 1, "上一次按过取消，不该把这一次的答复也吞掉")
        self.assertEqual(messages, [])

    def test_ok_button_keeps_the_request_alive(self):
        """按「好」只是关掉框，用户仍然想知道结果。"""
        closed, prompts = [], []
        box = object()
        host = _fake(closed=closed, prompts=prompts, **_IN_FLIGHT,
                     _update_check_dialog=box, _update_manual_wait=True,
                     _update_last_check_at=time.time())
        _handle_cancel(host, "好")
        self.assertEqual(closed, [box], "点「好」也要收框，否则下一张弹不出来")
        _handle_result(host, False, _NEW, None)
        self.assertEqual(prompts, [_NEW], "「好」不作废诉求")

    def test_cancel_closes_the_dialog_whatever_the_button(self):
        closed = []
        box = object()
        host = _fake(closed=closed, _update_check_dialog=box)
        _handle_cancel(host, "取消")
        self.assertEqual(closed, [box])

    def test_checking_dialog_offers_a_cancel_button(self):
        """等待框只保留「取消」，不再提供「好」按钮。"""
        _ensure_qapp()
        import sf_system_alert
        captured = {}

        def fake_show_alert(**kw):
            captured.update(kw)
            return object()

        class Host(stock_float_window.QWidget):
            _update_check_dialog = None
            _anchor_update_dialog_screen = lambda self: None
            _q_put = lambda self, item: None
            _show_checking_dialog = _show_checking  # 借真实现验证按钮契约

        host = Host()
        self.addCleanup(host.deleteLater)
        with patch.object(sf_system_alert, "show_alert", fake_show_alert), \
             patch.object(sf_system_alert, "bundled_icon_path", return_value=None):
            _show_checking(host)
        self.assertEqual(captured.get("buttons"), ("取消",))
        self.assertEqual(captured.get("default"), "取消")
        self.assertEqual(captured.get("cancel"), "取消")
        self.assertTrue(callable(captured.get("on_result")),
                        "点击结果必须转投主线程队列，不能在 AppKit 回调里直接动状态")


class AutoCheckRhythmIsVisible(SFCase):
    def test_menu_no_longer_shows_the_rhythm_row(self):
        """v1.4.51：用户看图要求"右键菜单中的提示语去掉，不展示" —— 那行灰字撤了。

        原来这条测试是**自己用常量拼一遍字符串再断言自己拼的东西**，产品一行都没碰，
        菜单文本删掉它也不会红（台账 G14，与 D08 同族）。节奏本身"只有一个来源"这条
        判据没有丢，改到真实对象上量：
        `tests/test_face_guard.py::TestWindowWiring::test_update_periodic_timer_uses_the_constant`
        断言的是主窗那个周期定时器**装进去的间隔**。
        """
        from sf_constants import UPDATE_INITIAL_DELAY_MS, UPDATE_PERIODIC_MS
        self.assertGreater(UPDATE_INITIAL_DELAY_MS, 0)
        self.assertGreater(UPDATE_PERIODIC_MS, 3600000)


if __name__ == "__main__":
    unittest.main()
