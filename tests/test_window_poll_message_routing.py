# -*- coding: utf-8 -*-
"""主窗 `_poll` 的消息路由与合批语义（覆盖率轮 C 档，2026-10-12）。

为什么挑这一处：`_poll` 包含两类最能骗过人的分支：

  · 缩小到 Dock 那一整段（`_minimized` 分支）——低频轮询期间仍要消费行情队列，
    坏掉的形态是"从 Dock 回来看到的是几分钟前的价"；
  · OTA 那五类消息（`update_check` / `update_check_cancel` / `update_prompt_answer` /
    `update_progress` / `update_download`）的路由与**先后顺序**。
    这段外面裹着 `except Exception`（AGENTS §6 第 9 条点名的正是这一族）：
    哪天把 `self._handle_update_download(*data)` 写成不存在的名字，运行时只留一行
    `poll error` 日志，界面表现为"点了检查更新没反应"，而全量回归一直是绿的。

合批语义也是判据而不是实现细节，因为它们是用户能看见的规则：
同批多条行情只应用最后一条；成功数据在后就覆盖掉失败态；
"取消"必须**先于**"结果"应用（源码注释里写死了这条口径 —— 让结果先跑就会弹一个
用户刚按掉的框）；进度只留最后一条。

手法沿用本仓既有做法（`tests/test_coverage_window_interactions.py`）：把被检方法绑到
一个只装配相关成员的宿主上，协作者用记录器。每条判据都是确定性的，不依赖屏幕上真
有窗口，也不会把真定时器/真网络拖进用例。

AGENTS §6 对号：不 sleep（R3，全部同步调用）、不依赖当下时钟（R4，交易时段由注入的
`_is_trading_time` 决定）、私有成员只走 `_wire`/`_peek` 两个入口（R5，本文件只有顶部
一枚豁免标记，锁的是被测对象本身）。
"""
from __future__ import annotations

import os
import queue
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import stock_float_window as m
from tests._base import SFCase

_QAPP = None


def _ensure_qapp():
    """QApplication 必须由模块级引用持有，否则离屏下会中途 abort（同 test_v1443 的做法）。"""
    global _QAPP
    if _QAPP is None:
        _QAPP = SFCase.ensure_qapp()
    return _QAPP


_poll = m.FloatWindow._poll          # discipline-ok(R5): 被测对象本身就是主窗的私有实现

# 注入的间隔值：断言"按哪一档算出来的间隔被设上"，不写死产品常量
IV_MIN = 5000
TIV_MIN = 3000
IV_NORMAL = 50
TIV_NORMAL = 10000
CODE_A = "sh600519"
STALE_POINTS = [(1, 2.0)]
FRESH_POINTS = [(1, 2.0), (2, 2.1)]


def _wire(host, **members):
    """把协作者/内部状态装到宿主上（成员名写在关键字参数里，理由见 §6 第 12 条）。"""
    for key, value in members.items():
        setattr(host, key, value)
    return host


def _peek(host, *names):
    values = [getattr(host, name) for name in names]
    return values[0] if len(values) == 1 else tuple(values)


class _Timer:
    """QTimer 的最小替身：`_poll` 只读 interval() 与写 setInterval()。"""

    def __init__(self, start=1):
        self._iv = start
        self.set_calls = []

    def interval(self):
        return self._iv

    def setInterval(self, ms):
        self.set_calls.append(ms)
        self._iv = ms


class _Host:
    """只装配 `_poll` 真正伸手拿的那些成员；其余一律不出现在夹具里，
    这样"漏装协作者"会当场炸出 TypeError 而不是被 Mock 的自动属性悄悄放过。"""

    def __init__(self):
        self.calls = []
        self.running = True
        self.code = CODE_A
        self.minute_points = []
        self._net_ok = True
        self._need_redraw = False
        self._line_ver = 0
        self._chart_changed = False
        self._width_dirty = False
        self.q = queue.Queue()
        self.timer = _Timer()
        self.top_timer = _Timer()
        # `_poll` 末尾把 self._poll 投给 QTimer.singleShot；宿主给一个可辨识的占位对象，
        # 这样"补的那一拍是不是自己"能断言，而 singleShot 已被 patch，占位永远不会真跑。
        self._poll = self._next_tick
        names = ("_apply_quote", "_apply_minute_points", "_handle_minute_stale",
                 "_handle_update_check", "_handle_update_check_cancel",
                 "_handle_update_prompt_answer", "_handle_update_progress",
                 "_handle_update_download", "_update_nap_policy", "_apply_icons",
                 "_repaint_now", "_sync_dot_timer", "_auto_width",
                 "_is_trading_time", "_icons_pending")
        for n in names:
            _wire(self, **{n: self._rec(n)})

    def _next_tick(self):
        raise AssertionError("测试里不该真跑下一拍（singleShot 必须被 patch 掉）")

    def _rec(self, name):
        calls = self.calls

        def _fn(*args):
            calls.append((name, args))
        _fn.calls = lambda: [c for c in calls if c[0] == name]
        return _fn

    def args_of(self, name):
        return [c[1] for c in self.calls if c[0] == name]

    def put(self, *items):
        for it in items:
            self.q.put(it)

    def order(self):
        return [c[0] for c in self.calls]


class PollRoutingCase(SFCase):
    """`_poll` 的路由表：每一类消息都必须交到对应的 handler，且参数形状正确。"""

    def setUp(self):
        _ensure_qapp()
        super().setUp()
        self.host = _Host()
        # 间隔决策函数是 sf_quote_loop 的纯函数，这里给确定返回值：
        # 本文件判的是"取的是哪一档、有没有设上"，不是那档等于几毫秒
        self.track_patch(mock.patch.object(m, "ui_poll_interval_ms",
                                           return_value=IV_MIN))
        self.track_patch(mock.patch.object(m, "ui_top_poll_interval_ms",
                                           return_value=TIV_MIN))
        self.track_patch(mock.patch.object(m, "log_line", lambda *a, **k: None))
        # singleShot 会在真事件循环里排队 —— 记下来就够，别让它回头再跑一次 _poll
        self.singles = []
        self.track_patch(mock.patch.object(
            m.QTimer, "singleShot",
            classmethod(lambda cls, ms, fn: self.singles.append((ms, fn)))))

    def minimized(self, iv=IV_MIN, tiv=TIV_MIN):
        self.track_patch(mock.patch.object(m, "ui_poll_interval_ms", return_value=iv))
        self.track_patch(mock.patch.object(m, "ui_top_poll_interval_ms", return_value=tiv))
        _wire(self.host, _minimized=True)

    def normal(self, trading=False):
        _wire(self.host, _minimized=False)
        _wire(self.host, **{"_is_trading_time": lambda: trading})

    # ---- 退出闸门 ----

    def test_退出流程里任何一拍都不再动界面(self):
        _wire(self.host, running=False)
        self.host.put(("quote", (CODE_A, {"price": "1"})))
        _poll(self.host)
        self.assertEqual(self.host.calls, [], "running=False 还消费队列 = 退出后残留 UI 更新")

    # ---- 缩到 Dock 那一整段 ----

    def test_缩Dock期间队列照样消化但走低频(self):
        self.minimized()
        self.host.put(("quote", (CODE_A, {"price": "1.5"})))
        _poll(self.host)
        self.assertEqual(self.host.args_of("_apply_quote"), [({"price": "1.5"},)])
        self.assertIn(IV_MIN, self.host.timer.set_calls, "低频间隔没设上")
        self.assertIn(TIV_MIN, self.host.top_timer.set_calls)
        self.assertEqual(self.host.args_of("_update_nap_policy"), [(False,)])

    def test_缩Dock分支不碰常规段的状态(self):
        """缩 Dock 段自己 `return`；哪天漏了这句，恢复时会带着常规段的合并状态跑一遍。"""
        self.minimized()
        _poll(self.host)
        self.assertEqual(self.host.args_of("_sync_dot_timer"), [],
                         "缩 Dock 分支走到了常规分支的收尾")

    def test_缩Dock期间五类更新消息各归各的handler(self):
        self.minimized()
        self.host.put(
            ("update_check", ("ok", "有新版")),
            ("update_check_cancel", "skip"),
            ("update_prompt_answer", "later"),
            ("update_progress", (50, "下载中")),
            ("update_download", ("done", "/tmp/x")),
        )
        _poll(self.host)
        self.assertEqual(self.host.args_of("_handle_update_check"), [("ok", "有新版")])
        self.assertEqual(self.host.args_of("_handle_update_check_cancel"), [("skip",)])
        self.assertEqual(self.host.args_of("_handle_update_prompt_answer"), [("later",)])
        self.assertEqual(self.host.args_of("_handle_update_progress"), [(50, "下载中")])
        self.assertEqual(self.host.args_of("_handle_update_download"),
                         [("done", "/tmp/x")],
                         "*data 展开写错 = 点检查更新只有一行 poll error")

    def test_缩Dock期间分时与滞后态也即时应用(self):
        self.minimized()
        self.host.put(
            ("minute", (CODE_A, FRESH_POINTS)),
            ("minute_stale", CODE_A),
            ("net", False),
        )
        _poll(self.host)
        self.assertEqual(self.host.args_of("_apply_minute_points"), [(FRESH_POINTS,)])
        self.assertTrue(_peek(self.host, "_chart_changed"), "整表替换没标记图表变化")
        self.assertEqual(self.host.args_of("_handle_minute_stale"), [(CODE_A,)])
        self.assertIs(_peek(self.host, "_net_ok"), False)

    def test_缩Dock期间迟到回包按代码丢弃(self):
        self.minimized()
        self.host.put(("quote", ("sz000001", {"price": "9"})),
                      ("minute", ("sz000001", [(9, 9)])))
        _poll(self.host)
        self.assertEqual(self.host.args_of("_apply_quote"), [])
        self.assertEqual(self.host.args_of("_apply_minute_points"), [])

    def test_缩Dock期间兼容不带代码的旧载荷(self):
        self.minimized()
        self.host.put(("quote", {"price": "2"}), ("minute", STALE_POINTS))
        _poll(self.host)
        self.assertEqual(self.host.args_of("_apply_quote"), [({"price": "2"},)])
        self.assertEqual(self.host.args_of("_apply_minute_points"), [(STALE_POINTS,)])

    def test_缩Dock期间队列异常只留日志不外抛(self):
        """`_poll` 由定时器驱动：往外抛等于每次抖动都可能把主线程带下去，
        而只吞不记等于"功能不见了但没有任何痕迹"（AGENTS §6 第 9 条那一族）。"""
        self.minimized()
        boom = queue.Queue()
        boom.put(("这一项只有一个元素",))            # `kind, data = item` 这一刻就炸
        boom.put(("quote", (CODE_A, {"price": "1"})))
        _wire(self.host, q=boom)
        seen = []
        self.track_patch(mock.patch.object(m, "log_line",
                                           lambda *a, **k: seen.append(a)))
        _poll(self.host)                               # 不许把异常抛给定时器
        self.assertTrue(any("poll error (minimized)" in str(x) for x in seen),
                        "异常被吞但没留痕 = 下一轮又查不到")
        left = boom.qsize()          # 先取成具名值：R6 只认"裸数字对几何属性"的形状
        self.assertEqual(left, 1, "炸之前那条已取出，剩下这条要留到下一拍")
        self.assertEqual(self.host.args_of("_apply_quote"), [])

    def test_间隔没变时不重复写定时器(self):
        """`interval() != iv` 才写：每拍都 setInterval 会让 Qt 重置计时，低频档形同虚设。"""
        self.minimized()
        _poll(self.host)
        first = list(self.host.timer.set_calls)
        _poll(self.host)
        self.assertEqual(self.host.timer.set_calls, first, "第二拍又写了一次间隔")

    def test_队列空时一个handler都不碰(self):
        self.minimized()
        _poll(self.host)
        self.assertEqual([c for c in self.host.calls if c[0].startswith("_handle")], [],
                         "空队列也走 handler = queue.Empty 那条支路没被走到")

    # ---- 常规分支：合批语义 ----

    def test_同批多条行情只应用最后一条(self):
        self.normal()
        self.host.put(("quote", (CODE_A, {"price": "1"})),
                      ("quote", (CODE_A, {"price": "2"})),
                      ("quote", (CODE_A, {"price": "3"})))
        _poll(self.host)
        self.assertEqual(self.host.args_of("_apply_quote"), [({"price": "3"},)],
                         "每条都应用 = 目标价判断与动画被重复触发")

    def test_成功数据在后就覆盖失败态(self):
        self.normal()
        self.host.put(("minute_stale", CODE_A), ("minute", (CODE_A, FRESH_POINTS)))
        _poll(self.host)
        self.assertEqual(self.host.args_of("_handle_minute_stale"), [],
                         "已有新分时还报滞后 = 图明明好了界面仍说重试中")
        self.assertEqual(self.host.args_of("_apply_minute_points"), [(FRESH_POINTS,)])

    def test_失败态在后就按失败处理(self):
        self.normal()
        self.host.put(("minute", (CODE_A, FRESH_POINTS)), ("minute_stale", CODE_A))
        _poll(self.host)
        self.assertEqual(self.host.args_of("_handle_minute_stale"), [(CODE_A,)])

    def test_网络状态只在翻转时重绘(self):
        self.normal()
        _wire(self.host, **{"_net_ok": True})
        self.host.put(("net", True))
        _poll(self.host)
        self.assertEqual(self.host.args_of("_repaint_now"), [], "没变化也不该重绘")
        self.host.put(("net", False))
        _poll(self.host)
        self.assertIs(_peek(self.host, "_net_ok"), False)
        self.assertEqual(len(self.host.args_of("_repaint_now")), 1)

    def test_取消必须先于结果应用(self):
        """同批里既有"取消"又有回包时，用户的意思是不再等 —— 顺序反了就会弹一个他刚按掉的框。"""
        self.normal()
        self.host.put(("update_check", ("ok", "有新版")),
                      ("update_check_cancel", "skip"))
        _poll(self.host)
        order = self.host.order()
        self.assertLess(order.index("_handle_update_check_cancel"),
                        order.index("_handle_update_check"))

    def test_进度只保留最新一条(self):
        self.normal()
        self.host.put(("update_progress", (10, "a")), ("update_progress", (90, "b")))
        _poll(self.host)
        self.assertEqual(self.host.args_of("_handle_update_progress"), [(90, "b")],
                         "把每个中间值都刷一遍 = 进度条抖动且拖慢主线程")

    def test_多次询问答案逐个应用(self):
        self.normal()
        self.host.put(("update_prompt_answer", "now"), ("update_prompt_answer", "later"))
        _poll(self.host)
        self.assertEqual(self.host.args_of("_handle_update_prompt_answer"),
                         [("now",), ("later",)])

    def test_分时整表替换要标记图表变化(self):
        self.normal()
        _wire(self.host, minute_points=STALE_POINTS)
        self.host.put(("minute", (CODE_A, FRESH_POINTS)))
        _poll(self.host)
        self.assertTrue(_peek(self.host, "_chart_changed"))
        self.assertTrue(_peek(self.host, "_need_redraw") or self.host.args_of("_repaint_now"),
                        "重绘请求既没置位也没发生")
        self.assertEqual(self.host.args_of("_apply_minute_points"), [(FRESH_POINTS,)])

    def test_收起态收盘后消化完队列要立刻补一拍(self):
        self.normal(trading=False)
        self.host.put(("quote", (CODE_A, {"price": "1"})))
        _poll(self.host)
        self.assertEqual(len(self.singles), 1, "非交易时段不补拍 = 首条行情要等 1 秒才上屏")
        self.assertIs(self.singles[0][1], _peek(self.host, "_poll"),
                      "补的那一拍不是这个窗自己的 _poll")

    def test_交易时段不再额外投递空轮询(self):
        self.normal(trading=True)
        self.host.put(("quote", (CODE_A, {"price": "1"})))
        _poll(self.host)
        self.assertEqual(self.singles, [], "每条消息都投一次 singleShot = 功耗回归 v1.4.10 那条")

    def test_宽度脏标记在批末才结算(self):
        self.normal()
        _wire(self.host, **{"_width_dirty": True})
        _poll(self.host)
        self.assertEqual(len(self.host.args_of("_auto_width")), 1)
        self.assertFalse(_peek(self.host, "_width_dirty"), "标志没清 = 每拍都重算布局")

    def test_未知类别不影响其它消息(self):
        self.normal()
        self.host.put(("some_future_kind", "x"), ("quote", (CODE_A, {"price": "7"})))
        _poll(self.host)
        self.assertEqual(self.host.args_of("_apply_quote"), [({"price": "7"},)])

    def test_图标待应用时才走图标支路(self):
        self.normal()
        _wire(self.host, **{"_icons_pending": lambda: True})
        _poll(self.host)
        self.assertEqual(len(self.host.args_of("_apply_icons")), 1)

    def test_常规段解包失败只留一行日志且批末收尾照做(self):
        """异常发生在**消费队列**那一段（在 try 里）：吞掉并留痕，本拍的收尾不能跟着丢。"""
        self.normal()
        boom = queue.Queue()
        boom.put(("这一项只有一个元素",))
        boom.put(("quote", (CODE_A, {"price": "5"})))
        _wire(self.host, q=boom)
        seen = []
        self.track_patch(mock.patch.object(m, "log_line",
                                           lambda *a, **k: seen.append(a)))
        _poll(self.host)
        self.assertTrue(any("poll error" in str(x) for x in seen),
                        "异常被吞但没留痕 = 用户报障时日志里一片干净")
        left = boom.qsize()
        self.assertEqual(left, 1, "炸之前那条已取出，剩下这条要留到下一拍")
        self.assertEqual(self.host.args_of("_apply_quote"), [],
                         "同批后面的消息不该在这一拍被应用 —— 它们没被读出来")
        self.assertEqual(len(self.host.args_of("_sync_dot_timer")), 1,
                         "批末收尾必须仍执行，否则呼吸灯会跟着这一次异常一起停")

OTA_MESSAGES = (
    ("update_check_cancel", "skip", ("skip",)),
    ("update_check", ("ok", "有新版"), ("ok", "有新版")),
    ("update_prompt_answer", "later", ("later",)),
    ("update_progress", (90, "下载中"), (90, "下载中")),
    ("update_download", ("done", "/tmp/test-update.zip"),
     ("done", "/tmp/test-update.zip")),
)


class PollHandlerFailureMatrix(SFCase):
    """只使用隔离 Host；不建 FloatWindow，不启动线程、网络或真实 OTA。"""

    normal = PollRoutingCase.normal
    minimized = PollRoutingCase.minimized

    def setUp(self):
        super().setUp()
        _ensure_qapp()
        self.host = _Host()
        self.singles = []
        self.track_patch(mock.patch.object(m, "ui_poll_interval_ms", return_value=IV_MIN))
        self.track_patch(mock.patch.object(m, "ui_top_poll_interval_ms", return_value=TIV_MIN))
        self.track_patch(mock.patch.object(m, "log_line", return_value=None))
        self.track_patch(mock.patch.object(
            m.QTimer, "singleShot",
            classmethod(lambda cls, ms, fn: self.singles.append((ms, fn)))))

    def _fail_once(self, kind):
        name = "_handle_" + kind
        record = self.host._rec(name)  # discipline-ok(R5): 隔离记录器记录失败调用，防止漏路由也被当通过
        failures = [True]

        def handler(*args):
            record(*args)
            if failures.pop() if failures else False:
                raise RuntimeError("matrix-boom:" + kind)

        _wire(self.host, **{name: handler})

    def _assert_delivered(self, messages):
        for kind, _data, args in messages:
            self.assertEqual(self.host.args_of("_handle_" + kind), [args], kind)

    def test_五类OTA异常逐条隔离且下一拍恢复(self):
        for trading in (False, True):
            for failed_kind, _data, _args in OTA_MESSAGES:
                with self.subTest(trading=trading, failed=failed_kind):
                    self.host = _Host()
                    self.singles.clear()
                    self.normal(trading=trading)
                    self._fail_once(failed_kind)
                    seen = []
                    with mock.patch.object(m, "log_line", side_effect=lambda *a: seen.append(a)):
                        _wire(self.host, _need_redraw=True, _width_dirty=True)
                        self.host.put(*[(kind, data) for kind, data, _args in OTA_MESSAGES])
                        _poll(self.host)
                        self._assert_delivered(OTA_MESSAGES)
                        self.assertTrue(self.host.q.empty())
                        self.assertEqual(len(seen), 1, seen)
                        self.assertIn("matrix-boom:" + failed_kind, str(seen))
                        self.assertIn("poll apply error", str(seen))
                        self.assertEqual(self.host.order()[-3:],
                                         ["_repaint_now", "_sync_dot_timer", "_auto_width"])
                        self.assertFalse(_peek(self.host, "_need_redraw"))
                        self.assertFalse(_peek(self.host, "_width_dirty"))
                        self.assertEqual(len(self.singles), int(not trading))
                        if not trading:
                            self.assertIs(self.singles[0][1], _peek(self.host, "_poll"))
                        self.host.calls.clear()
                        self.singles.clear()
                        _wire(self.host, _need_redraw=True, _width_dirty=True)
                        self.host.put(*[(kind, data) for kind, data, _args in OTA_MESSAGES])
                        _poll(self.host)
                        self._assert_delivered(OTA_MESSAGES)
                        self.assertEqual(len(seen), 1, "下一拍不应再次失败")
                        self.assertEqual(self.host.order()[-3:],
                                         ["_repaint_now", "_sync_dot_timer", "_auto_width"])

    def test_缩Dock五类OTA异常留队下一拍继续且恢复正常收尾(self):
        for failed in OTA_MESSAGES:
            with self.subTest(failed=failed[0]):
                self.host = _Host()
                self.singles.clear()
                self.minimized()
                self._fail_once(failed[0])
                remaining = [item for item in OTA_MESSAGES if item != failed]
                seen = []
                with mock.patch.object(m, "log_line", side_effect=lambda *a: seen.append(a)):
                    _wire(self.host, _need_redraw=True, _width_dirty=True)
                    self.host.put((failed[0], failed[1]),
                                  *[(kind, data) for kind, data, _args in remaining])
                    _poll(self.host)
                    self._assert_delivered([failed])
                    for kind, _data, _args in remaining:
                        self.assertEqual(self.host.args_of("_handle_" + kind), [])
                    self.assertEqual(self.host.q.qsize(), len(remaining))
                    self.assertEqual(len(seen), 1, seen)
                    self.assertIn("poll error (minimized)", str(seen))
                    self.assertIn("matrix-boom:" + failed[0], str(seen))
                    _poll(self.host)
                    self._assert_delivered(OTA_MESSAGES)
                    self.assertTrue(self.host.q.empty())
                    self.assertEqual(self.host.args_of("_update_nap_policy"), [(False,), (False,)])
                    self.assertEqual(self.host.timer.interval(), IV_MIN)
                    self.assertEqual(self.host.top_timer.interval(), TIV_MIN)
                    for tail in ("_repaint_now", "_sync_dot_timer", "_auto_width"):
                        self.assertEqual(self.host.args_of(tail), [], "缩 Dock 不执行常规收尾")
                    self.assertTrue(_peek(self.host, "_need_redraw"))
                    self.assertTrue(_peek(self.host, "_width_dirty"))
                    self.assertEqual(self.singles, [])
                    self.host.calls.clear()
                    self.normal(trading=True)
                    self.host.put((failed[0], failed[1]))
                    _poll(self.host)
                    self._assert_delivered([failed])
                    self.assertEqual(len(seen), 1, "恢复后 handler 应可再次成功执行")
                    self.assertEqual(self.host.order()[-3:],
                                     ["_repaint_now", "_sync_dot_timer", "_auto_width"])


class PollTailSurvivesHandlerErrors(SFCase):
    """v1.4.66 复核修复的判据：批末应用段纳入兜异常之后，收尾不可被跳过。

    修复前：任一 handler 抛错 → 异常沿 _poll 上抛，本拍的重绘/呼吸灯/宽度收尾全部
    不执行，界面只表现为"点了检查更新没反应"。改坏方向有两个，判据各钉一头：
    ① 把 try/except 拆掉（异常外抛）② 收尾搬回 try 里（异常后照样不执行）。
    """

    def _host_with_boom(self):
        _ensure_qapp()
        host = _Host()
        self.track_patch(mock.patch.object(m, "ui_poll_interval_ms", return_value=IV_MIN))
        self.track_patch(mock.patch.object(m, "ui_top_poll_interval_ms", return_value=TIV_MIN))
        seen = []
        self.track_patch(mock.patch.object(m, "log_line", lambda *a, **k: seen.append(a)))
        self.track_patch(mock.patch.object(
            m.QTimer, "singleShot",
            classmethod(lambda cls, ms, fn: None)))
        _wire(host, _minimized=False, **{"_is_trading_time": lambda: True})

        def boom(*args):
            raise AttributeError(" '_handle_update_check' is not defined")
        _wire(host, **{"_handle_update_check": boom})
        host.put(("update_check", ("ok", "有新版")))
        return host, seen

    def test_handler抛错不外抛且本拍收尾照做(self):
        host, seen = self._host_with_boom()
        _poll(host)                                  # 修复前：AttributeError 外抛
        self.assertTrue(any("poll apply error" in str(x) for x in seen),
                        "吞了异常却没留痕 = 下一轮又查不到")
        self.assertEqual(len(host.args_of("_repaint_now")), 0,
                         "本拍没有待重绘内容时不该凭空重绘（_need_redraw 未置位）")
        self.assertEqual(len(host.args_of("_sync_dot_timer")), 1,
                         "呼吸灯复核被跳过 = 动画状态从此没人兜底")

    def test_异常之后同批后续消息不丢(self):
        """解包失败那类异常发生在消费段（原有守卫内）；这里是应用段新守卫，
        两者语义一致：炸掉的那条不装死，本拍收尾照做，队列里剩下的留到下一拍。"""
        host, seen = self._host_with_boom()
        host.put(("update_progress", (90, "下载中")))
        _poll(host)
        self.assertTrue(any("poll apply error" in str(x) for x in seen))
        self.assertEqual(host.args_of("_handle_update_progress"), [(90, "下载中")],
                         "排在炸掉那一条之后的 handler 被跳过 = 更新进度凭空中断")


if __name__ == "__main__":
    unittest.main(verbosity=2)
