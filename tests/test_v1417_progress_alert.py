# -*- coding: utf-8 -*-
"""v1.4.17 下载进度框原生化回归：系统弹窗 + 系统进度条，Qt 框退居兜底。

用户要求（2026-09-22）："下载进度框也改成系统原生的样式"。实现是 NSAlert 挂一条
`NSProgressIndicator` 作为 accessoryView（系统自带的确定型进度条）。两条底线：

1. **绝不降级成 osascript** —— AppleScript 的 display dialog 装不下进度条，
   弹一个"看不到进展"的框比样式不原生更糟；原生不可用时必须返回 None，
   由调用方退回原来的 Qt 自绘框（进度反馈不能消失）。
2. **Qt 兜底路径的析构顺序不得回退** —— 历史上把 Qt 框的销毁留给
   `QApplication.quit()` 会撞上 QWindow/QSurface 与延迟删除事件交错的 SIGSEGV，
   所以是"先撤引用 → close() → 就地销毁"。v1.4.20 起第三步从 deleteLater()
   改成 sip.delete()：macOS 上 quit() 等于 -[NSApplication terminate:] → exit()，
   排队的延迟删除会被推到 C++ 静态析构阶段才执行，那时销毁 QWindow 仍是空指针。
   真销毁的断言见 tests/test_update.py 与 tests/test_v1420_exit_teardown.py。

本文件用桩 AppKit 与桩 Qt 控件，测试环境不建任何真窗口（真弹窗链路见
tests/test_v1417_alert_chain.py）。
"""

import os
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sf_system_alert
import stock_float_window


# --------------------------------------------------------------------------
# 桩 AppKit：够 NativeAlert(progress=True) 跑起来
# --------------------------------------------------------------------------

class _Button:
    def __init__(self, title):
        self.title_text = title
        self.key = None
        self.target = None
        self.action = None

    def title(self):
        return self.title_text

    def setKeyEquivalent_(self, key):
        self.key = key

    def setFocusRingType_(self, value):
        self.focus_ring = value

    def setTarget_(self, target):
        self.target = target

    def setAction_(self, action):
        self.action = action


class _Window:
    def __init__(self):
        self.calls = []

    def setReleasedWhenClosed_(self, value):
        self.calls.append(("released_when_closed", value))

    def collectionBehavior(self):
        return 0

    def setCollectionBehavior_(self, value):
        self.calls.append(("behavior", value))

    def setLevel_(self, value):
        self.calls.append(("level", value))

    def setHidesOnDeactivate_(self, value):
        self.calls.append(("hides", value))

    def isOnActiveSpace(self):
        return True

    def makeKeyAndOrderFront_(self, _):
        self.calls.append(("front",))

    def orderOut_(self, _):
        self.calls.append(("order_out",))


class _Bar:
    """NSProgressIndicator 替身：记录被设过的值与刷新次数。"""

    @classmethod
    def alloc(cls):
        return cls()

    def initWithFrame_(self, frame):
        self.frame = frame
        return self

    def init(self):
        return self

    def setStyle_(self, style):
        self.style = style

    def setIndeterminate_(self, value):
        self.indeterminate = value

    def setMinValue_(self, value):
        self.minimum = value

    def setMaxValue_(self, value):
        self.maximum = value

    def setDoubleValue_(self, value):
        self.values.append(value)

    def display(self):
        self.displays += 1

    def __init__(self):
        self.values = []
        self.displays = 0
        self.indeterminate = None


class _Alert:
    instances = []

    @classmethod
    def alloc(cls):
        return cls()

    def __init__(self):
        self.message = ""
        self.info = ""
        self.accessory = None
        self.buttons_list = []
        self.window_obj = _Window()
        self.layouts = 0
        _Alert.instances.append(self)

    def init(self):
        return self

    def setMessageText_(self, value):
        self.message = value

    def setInformativeText_(self, value):
        self.info = value

    def setAccessoryView_(self, view):
        self.accessory = view

    def accessoryView(self):
        return self.accessory

    def addButtonWithTitle_(self, title):
        button = _Button(title)
        self.buttons_list.append(button)
        return button

    def buttons(self):
        return self.buttons_list

    def layout(self):
        self.layouts += 1

    def window(self):
        return self.window_obj


class _Target:
    @classmethod
    def alloc(cls):
        return cls()

    def init(self):
        return self

    def setOwner_(self, owner):
        self.owner = owner


class _FakeAppKit:
    NSWindowCollectionBehaviorFullScreenAuxiliary = 1 << 8
    NSAlert = _Alert
    NSProgressIndicator = _Bar
    NSImage = None
    NSApp = None
    NSProgressIndicatorStyleBar = 0
    NSFocusRingTypeNone = 1      # 与真 AppKit 同值：按钮不要焦点描边

    @staticmethod
    def NSMakeRect(x, y, w, h):
        return (x, y, w, h)


class ProgressAlertBridgeTest(unittest.TestCase):
    """sf_system_alert 侧：进度条挂上去、刷新、收口后不再接受刷新。"""

    def setUp(self):
        _Alert.instances = []
        self.patches = [
            patch.object(sf_system_alert, "AppKit", _FakeAppKit),
            patch.object(sf_system_alert, "_AlertTarget", _Target),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])

    def _open(self, on_result=None):
        return sf_system_alert.show_progress_alert(
            message="正在下载更新…", info="准备中 0%", on_result=on_result)

    def test_system_progress_bar_is_mounted_as_accessory(self):
        handle = self._open()
        self.assertIsInstance(handle, sf_system_alert.NativeAlert)
        alert = _Alert.instances[-1]
        self.assertIsInstance(alert.accessory, _Bar, "进度条必须是系统控件本身")
        self.assertEqual(alert.accessory.indeterminate, False,
                         "确定型：用户要看得到还剩多少，不是转圈")
        self.assertEqual((alert.accessory.minimum, alert.accessory.maximum), (0.0, 100.0))
        self.assertEqual([b.title() for b in alert.buttons()], ["取消"])
        self.assertEqual(alert.buttons()[0].key, "\x1b", "单按钮也要绑 Esc=取消")

    def test_set_progress_moves_bar_and_rewrites_subtext(self):
        handle = self._open()
        alert = _Alert.instances[-1]
        self.assertTrue(handle.set_progress(37, "37%（9.9 / 26.8 MB）"))
        self.assertEqual(alert.accessory.values[-1], 37.0)
        self.assertEqual(alert.info, "37%（9.9 / 26.8 MB）")
        self.assertGreaterEqual(alert.accessory.displays, 1)

    def test_same_percentage_does_not_reflow_the_dialog(self):
        """下载分片很密：同一条进度不得让 AppKit 反复重排弹窗。"""
        handle = self._open()
        alert = _Alert.instances[-1]
        layouts_before = alert.layouts
        self.assertTrue(handle.set_progress(40, "40%"))
        after_first = alert.layouts
        self.assertFalse(handle.set_progress(40, "40%"), "重复进度必须被跳过")
        self.assertEqual(alert.layouts, after_first, "跳过时不得再排一次版")
        self.assertGreater(alert.layouts, layouts_before)

    def test_progress_value_is_clamped_into_the_bar_range(self):
        handle = self._open()
        bar = _Alert.instances[-1].accessory
        handle.set_progress(150, "150%")
        handle.set_progress(-5, "-5%")
        self.assertEqual(bar.values[-2:], [100.0, 0.0], "超范围必须夹进 0~100")

    def test_closed_handle_refuses_further_progress(self):
        handle = self._open()
        bar = _Alert.instances[-1].accessory
        seen = len(bar.values)
        handle.close()
        self.assertFalse(handle.set_progress(80, "80%"),
                         "收口后的迟到进度必须被丢弃，不能再碰已销毁的控件")
        self.assertEqual(len(bar.values), seen, "收口后不得再动进度条")


class NoOsascriptFallbackTest(unittest.TestCase):
    """原生不可用时要返回 None，绝不能退回"没有进度条的 osascript 弹窗"。"""

    def test_returns_none_without_native_path(self):
        with patch.object(sf_system_alert, "native_available", lambda: False), \
             patch.object(sf_system_alert, "SystemDialog",
                          side_effect=AssertionError("不该降级成 osascript")):
            self.assertIsNone(sf_system_alert.show_progress_alert(message="x"))

    def test_appkit_failure_also_yields_none_not_a_dialog(self):
        with patch.object(sf_system_alert, "native_available", lambda: True), \
             patch.object(sf_system_alert, "NativeAlert",
                          side_effect=RuntimeError("桥接炸了")), \
             patch.object(sf_system_alert, "SystemDialog",
                          side_effect=AssertionError("不该降级成 osascript")):
            self.assertIsNone(sf_system_alert.show_progress_alert(message="x"))


# --------------------------------------------------------------------------
# 调用点侧：优先原生、退回 Qt、取消语义、收口顺序
# --------------------------------------------------------------------------

class _Box:
    """QMessageBox 替身。"""

    instances = []

    class Icon:
        Information = "information"

    class StandardButton:
        NoButton = 0

    class ButtonRole:
        RejectRole = 1

    def __init__(self, parent=None):
        self.parent = parent
        self.events = []
        self.layout_obj = _Layout()
        _Box.instances.append(self)

    def setIcon(self, value):
        self.events.append(("icon", value))

    def setWindowTitle(self, value):
        self.events.append(("title", value))

    def setText(self, value):
        self.events.append(("text", value))

    def setStandardButtons(self, value):
        self.events.append(("buttons", value))

    def layout(self):
        return self.layout_obj

    def addButton(self, title, role):
        button = _QButton(title)
        self.events.append(("add", title))
        return button

    def show(self):
        self.events.append(("show",))

    def close(self):
        self.events.append(("close",))

    def deleteLater(self):
        self.events.append(("deleteLater",))


class _Signal:
    """pyqtSignal 替身：只记连上去的槽。"""

    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def fire(self):
        for slot in self.slots:
            slot()


class _QButton:
    def __init__(self, title):
        self.title = title
        self.clicked = _Signal()


class _Layout:
    def columnCount(self):
        return 1

    def addWidget(self, *args):
        self.added = getattr(self, "added", 0) + 1


class _BarQt:
    def __init__(self, parent=None):
        self.parent = parent
        self.range = None
        self.value = None

    def setRange(self, low, high):
        self.range = (low, high)

    def setValue(self, value):
        self.value = value


class _NativeHandle:
    """原生进度框句柄替身：只有 close() 与 set_progress()，没有 deleteLater。"""

    def __init__(self, fail=False):
        self.progress = []
        self.closes = 0
        self.fail = fail

    def set_progress(self, pct, text=""):
        if self.fail:
            raise RuntimeError("appkit gone")
        self.progress.append((pct, text))
        return True

    def close(self):
        self.closes += 1


class _Host:
    """只带下载进度框用到的属性，但**借用真实方法**（不构造浮窗）。

    _open_download_progress_box 会引用 self._on_download_dialog_answer 等兄弟方法，
    只塞属性的假对象会在"取回调"这一步就炸 —— 挂上真方法才测得到真实接线。
    """

    _cancel_update_download = stock_float_window.FloatWindow._cancel_update_download
    _on_download_dialog_answer = stock_float_window.FloatWindow._on_download_dialog_answer
    # v1.4.38：开框之前要先登记「弹在哪块屏」，兜底框还要按屏居中。两个真方法都带
    # 「宿主不是 QWidget 就什么都不做」的守卫，这里直接借真的，不另写一份假行为。
    _anchor_update_dialog_screen = stock_float_window.FloatWindow._anchor_update_dialog_screen
    _center_on_own_screen = stock_float_window.FloatWindow._center_on_own_screen

    def __init__(self):
        self._update_flow_box = None
        self._update_flow_progress = None
        self._update_flow_cancel = None
        self._update_cancel = threading.Event()


class DownloadProgressBoxTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import stock_float_window
        cls.win = stock_float_window

    def _open(self, host, native=None):
        with patch.object(sf_system_alert, "show_progress_alert",
                          return_value=native):
            return self.win.FloatWindow._open_download_progress_box(host)

    def test_native_alert_is_preferred_and_has_no_qt_bar(self):
        host = _Host()
        handle = _NativeHandle()
        self.assertIs(self._open(host, native=handle), handle)
        self.assertIs(host._update_flow_box, handle)
        self.assertIsNone(host._update_flow_progress,
                          "原生路径不该再留一条 Qt 进度条引用")
        self.assertIsNone(host._update_flow_cancel)

    def test_qt_box_is_the_fallback_and_keeps_cancel_wiring(self):
        host = _Host()
        _Box.instances = []
        with patch.object(self.win, "QMessageBox", _Box), \
             patch.object(self.win, "QProgressBar", _BarQt):
            box = self._open(host, native=None)
        self.assertIsInstance(box, _Box)
        self.assertEqual(box.layout_obj.added, 1, "进度条要加进弹窗布局")
        cancel = host._update_flow_cancel
        self.assertEqual(cancel.title, "取消")
        self.assertFalse(host._update_cancel.is_set())
        cancel.clicked.fire()
        self.assertTrue(host._update_cancel.is_set(),
                        "兜底框的取消仍必须真的能停下载，别因为原生化丢掉")

    def test_progress_goes_to_whichever_box_is_on_screen(self):
        host = _Host()
        handle = _NativeHandle()
        host._update_flow_box = handle
        self.win.FloatWindow._handle_update_progress(host, 47, 5 * 1048576,
                                                     10 * 1048576)
        self.assertEqual(handle.progress,
                         [(47, "47%（5.0 / 10.0 MB）")])

    def test_qt_fallback_wording_survives(self):
        host = _Host()
        box, bar = _Box(), _BarQt()
        host._update_flow_box, host._update_flow_progress = box, bar
        self.win.FloatWindow._handle_update_progress(host, 12, 1048576, 8 * 1048576)
        self.assertEqual(bar.value, 12)
        shown = [ev[1] for ev in box.events if ev[0] == "text"]
        self.assertEqual(shown, ["正在下载更新… 12%（1.0 / 8.0 MB）"])

    def test_clamped_and_no_box_is_harmless(self):
        host = _Host()
        host._update_flow_box = None
        self.win.FloatWindow._handle_update_progress(host, 999, 0, 0)
        self.assertIsNone(host._update_flow_progress)

    def test_native_refresh_failure_logs_and_does_not_escape(self):
        """原生刷新抛异常：留一条 WARNING，且绝不把异常抛回事件队列。"""
        host = _Host()
        host._update_flow_box = _NativeHandle(fail=True)   # 原生路径本就没有 Qt 条
        logs = []
        with patch.object(self.win, "log_line",
                          side_effect=lambda msg, level="INFO": logs.append((level, msg))):
            self.win.FloatWindow._handle_update_progress(host, 61, 2 * 1048576,
                                                         3 * 1048576)
        self.assertEqual([level for level, _ in logs], ["WARNING"],
                         "刷新失败要留痕，否则线上只看到进度不动、也查不到原因""")

    def test_fallback_to_qt_bar_only_when_a_qt_bar_exists(self):
        """退回 Qt 写法的前提是真的有 Qt 进度条与 setText：不凭空造引用。"""
        host = _Host()
        box, bar = _Box(), _BarQt()
        host._update_flow_box = _NativeHandle(fail=True)
        host._update_flow_progress = bar            # 混用形态：框是原生、条是 Qt
        with patch.object(self.win, "log_line"):
            self.win.FloatWindow._handle_update_progress(host, 61, 2 * 1048576,
                                                         3 * 1048576)
        self.assertEqual(bar.value, None, "原生框没有 setText，不得半路去写它")

    def test_close_handles_native_and_qt_shapes(self):
        native = _NativeHandle()
        host = _Host()
        host._update_flow_box = native
        self.win.FloatWindow._close_update_flow_box(host)
        self.assertEqual(native.closes, 1, "原生句柄 close() 即收口")
        self.assertIsNone(host._update_flow_box)

        box = _Box()
        host2 = _Host()
        host2._update_flow_box = box
        self.win.FloatWindow._close_update_flow_box(host2)
        self.assertEqual([ev[0] for ev in box.events], ["close"],
                         "收口第一步永远是 close()。Qt 对象的销毁 v1.4.20 起改为就地 "
                         "sip.delete（deleteLater 会一路排到 exit() 的静态析构阶段才执行，"
                         "那时销毁 QWindow 即 SIGSEGV）；桩对象不是真 QWidget，所以这里"
                         "只锁 close 顺序，真销毁的断言在 tests/test_update.py 与 "
                         "tests/test_v1420_exit_teardown.py")

    def test_close_of_broken_box_logs_instead_of_raising(self):
        class _Bad:
            def close(self):
                raise RuntimeError("already gone")

        host = _Host()
        host._update_flow_box = _Bad()
        logs = []
        with patch.object(self.win, "log_line",
                          side_effect=lambda msg, level="INFO": logs.append((level, msg))):
            self.win.FloatWindow._close_update_flow_box(host)
        self.assertEqual([level for level, _ in logs], ["WARNING"])
        self.assertIsNone(host._update_flow_box, "收口失败也要先摘掉引用")

    def test_cancel_button_answer_stops_the_download(self):
        host = _Host()
        self.win.FloatWindow._on_download_dialog_answer(host, "取消")
        self.assertTrue(host._update_cancel.is_set())
        host2 = _Host()
        self.win.FloatWindow._on_download_dialog_answer(host2, "好")
        self.assertFalse(host2._update_cancel.is_set(),
                         "只认取消按钮，其他点击不得中断下载")


if __name__ == "__main__":
    unittest.main()
