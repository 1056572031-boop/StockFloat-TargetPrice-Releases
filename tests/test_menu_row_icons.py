# -*- coding: utf-8 -*-
"""右键菜单行首图标 + 「导出诊断包」文案（2026-10-13，用户看图要求）。

本机离屏实测（同一套 QSS，量 grab 出来的真帧）：

    无图标   文字起于 x32，勾号画在 x10..16；
    有图标   图标占行首图标列，文字起于 x43，**勾号那一整列变成空白**。

第二条就是本文件最要紧的一件事：Qt 让"勾选框"和"行首图标"共用同一格，
所以加了图标之后，「自动隐藏窗口」的开/关只能由图标本身说（睁眼 / 划掉的眼）。
它必须与 v1.4.67 定下的那一个口径 `_face_guard_switch_on()` 同源 —— 否则
"显示的是开"与"点下去会切成什么"又会各说各话，那正是 v1.4.67 刚修掉的缺陷。

判据分三层，每层都配反向对照（改坏必红）：
  1. 出图层：五张图都渲染得出来、两两不同、边长与 DPR 成对（v1.4.59 那条教训）；
  2. 行为层：四行各带一枚、版本行不带、眼睛图标随口径换、菜单开着时刷新也换、
     取不到图时退回"无图标 + 勾号"而不是两态皆盲；
  3. 像素层：真帧里图标确实落在文字左边，且开/关两态画的不是一张图。
"""
import os
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtCore import QPoint, QRect, Qt  # noqa: E402
from PyQt6.QtGui import (QColor, QContextMenuEvent, QIcon, QImage,  # noqa: E402
                         QPainter, QPixmap)
from PyQt6.QtWidgets import QApplication  # noqa: E402

import sf_icons  # noqa: E402
import sf_quote  # noqa: E402
import stock_float_window as m  # noqa: E402
from sf_constants import MENU_ICON_DEVICE_RATIO, MENU_ICON_PT  # noqa: E402

try:
    from _base import SFCase                                   # noqa: E402
except ImportError:                                            # 以包形式被 discover 导入时
    from tests._base import SFCase                             # noqa: E402


MENU_BG = QColor(38, 43, 58)      # 菜单底色，与 contextMenuEvent 里那条 rgba(38,43,58,.97) 同值
EYE_ON, EYE_OFF = "eye_open", "eye_closed"
ROWS_WITH_ICON = ("刷新行情", "自动隐藏窗口", "检查更新", "导出诊断包")


def _fake_http_get(url, **kw):
    if "hq.sinajs.cn" in url:
        return 'var hq_str_sh600519="贵州茅台,1500.0,1490.0,1510.0,1520.0,1480.0";'
    return ""


def _render(icon, px=24):
    """QIcon → 固定尺寸位图。比"两张图是不是一张"必须先摊到同一坐标系，
    否则源图带 DPR 标记时两边宽度不同，逐点比会整体错位。"""
    img = QImage(px, px, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    icon.paint(p, QRect(0, 0, px, px))
    p.end()
    return img


def _signature(icon):
    """图标指纹：16×16 网格的 alpha 采样。用来判"换没换"，不用来判"画得对不对"。"""
    img = _render(icon)
    step = max(img.width() // 16, 1)
    return tuple(img.pixelColor(x * step, y * step).alpha()
                 for y in range(16) for x in range(16))


def _icon_for(key):
    ic = sf_icons.menu_icon(key)
    return ic


def _row_runs(menu_img, rect):
    """这一行里有墨的列，压成 [(起, 止), …]（相对整张帧）。"""
    cols = []
    for x in range(menu_img.width()):
        for y in range(rect.y(), rect.y() + rect.height()):
            c = menu_img.pixelColor(x, y)
            if (abs(c.red() - MENU_BG.red()) + abs(c.green() - MENU_BG.green())
                    + abs(c.blue() - MENU_BG.blue())) > 60:
                cols.append(x)
                break
    runs = []
    for x in cols:
        if runs and x <= runs[-1][1] + 2:
            runs[-1] = (runs[-1][0], x)
        else:
            runs.append((x, x))
    return runs


def _icon_band_right_edge(menu, img):
    """图标列的右界 = **同一帧里**版本行（不放图标那一行）文字的起点。

    刻意不写死像素数：菜单宽度、字体度量、DPR 都会挪动文字位置，
    写死的数字换一台机器就只是在量别的东西。
    """
    version = [a for a in menu.actions() if not a.isSeparator() and not a.isEnabled()]
    assert version, "菜单里没有禁用行（版本行），取不到文字起点这个参照物"
    runs = _row_runs(img, menu.actionGeometry(version[0]))
    assert runs, "版本行一个像素都没画出来，这一帧没法当参照"
    return runs[0][0]


def _icon_ink_runs(menu, img, act, band_right):
    """act 这一行在图标列里的墨（空列表 = 这一行没画图标）。"""
    rect = menu.actionGeometry(act)
    return [(a, b) for a, b in _row_runs(img, rect) if a < band_right]


def _icon_band_ink(img, rect, band_right):
    """图标列那一块的"离底距离"逐点表 —— 用来比两帧画的是不是同一张图。

    刻意不用 `_row_runs` 的列游程：两枚眼睛占的**列**是一样的（都是整幅宽），
    按列压出来的结果相同，那种判据看得见"有没有画"、看不见"画的是什么"。
    """
    return tuple(abs(img.pixelColor(x, y).red() - MENU_BG.red())
                 + abs(img.pixelColor(x, y).green() - MENU_BG.green())
                 + abs(img.pixelColor(x, y).blue() - MENU_BG.blue())
                 for y in range(rect.y(), rect.y() + rect.height())
                 for x in range(rect.left(), band_right))


def _ink_difference(a, b):
    """两张逐点表里"差得明显"的点数（阈值与 _row_runs 认墨的阈值同口径）。"""
    return sum(1 for x, y in zip(a, b) if abs(x - y) > 60)


class _WindowCase(SFCase):
    """造一个真 FloatWindow 并把菜单弹出来。

    与 tests/test_v135.py 同一套补丁口径（网络、配置落盘、置顶、图标后台线程、
    reopen 处理器），差别只在**不 patch QMenu.popup**：像素层要看的就是真弹出来的那一帧。
    """

    @classmethod
    def setUpClass(cls):
        # QApplication 必须由用例**持有引用**，而且要挂在类上：局部变量接住的话
        # 用例一结束 app 就被 GC，下一句建 QWidget 直接 "Must construct a
        # QApplication before a QWidget"（本机实测撞过）。
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        super().setUp()
        # patch 清单照 tests/test_power_v1410._WinCase 抄全（AGENTS §6 第 14 条）：
        # 窗口不是被测对象，构造它的全部代价都是夹具的责任 —— 少一条就可能让
        # __init__ 真去起后台循环、真去 open 那个 bundle。
        self.track_patch(patch.object(
            m, "load_config",
            return_value=("sh600519", 100, 100, 200, "translucent")))
        self.track_patch(patch.object(m, "save_config", lambda *a, **k: True))
        self.track_patch(patch.object(sf_quote, "http_get", side_effect=_fake_http_get))
        self.track_patch(patch.object(m, "start_icon_worker", lambda *a, **k: None))
        self.track_patch(patch.object(m.FloatWindow, "_force_top", lambda self: None))
        self.track_patch(patch.object(m.FloatWindow, "_inject_glass", lambda self: None))
        self.track_patch(patch.object(m.FloatWindow, "_setup_reopen_handler",
                                      lambda self: None))
        self.track_patch(patch.object(m.FloatWindow, "raise_", lambda self: None))
        self.track_patch(patch.object(m.FloatWindow, "activateWindow", lambda self: None))
        self.track_patch(patch.object(m.FloatWindow, "_quote_loop_wrapper",
                                      lambda self: None))
        self.track_patch(patch.object(m.FloatWindow, "_minute_loop_wrapper",
                                      lambda self: None))
        self.win = self.track_widget(m.FloatWindow())
        self.win.running = False
        self.win._wake.set()                      # discipline-ok(R5): 停线程的公开开关就是 running，_wake 是它的配套唤醒，没有对外读法
        self.win._resched.set()                   # discipline-ok(R5): 同上，重排唤醒位没有对外接口
        self.win.timer.stop()                     # discipline-ok(R5): 常驻定时器没有对外停止口，不停会在用例之间自己刷新菜单

    def tearDown(self):
        # §6 第 11 条：关窗必须排在 patch 还活着的时候，否则 closeEvent 里的
        # _persist_on_exit 会走真实 save_config，把测试窗口位置写进用户配置。
        # 窗口已登记给 SFCase，这里提前关掉它 —— SFCase 再 close 一次是幂等的。
        try:
            self.win._exit_persisted = True       # discipline-ok(R5): 落盘幂等标记没有对外接口，offscreen 下 closeEvent 可能晚到
            self.win.close()
        finally:
            self.win.running = False
            for t in (self.win._minute_thread, self.win._quote_thread):   # discipline-ok(R5): 两条后台线程没有对外 join 口，靠这两个成员收口，否则跨用例残留
                t.join(2.0)
            super().tearDown()

    def open_menu(self):
        # 前面的用例（实测：test_popup_materials 里两条真弹层用例连着跑）会留下待派发的
        # 窗口激活/失活事件，它们在菜单 popup() 之后的 processEvents 里才到，Qt 把刚弹出的
        # 菜单当成失焦收掉，_ctx_menu 变回 None（seed=1464 整档稳定红，单跑和整模块都绿）。
        # 先排空一次，让本用例从干净的事件队列起步。
        QApplication.processEvents()
        self.win.contextMenuEvent(QContextMenuEvent(
            QContextMenuEvent.Reason.Mouse, QPoint(40, 40), QPoint(140, 140)))
        QApplication.processEvents()
        menu = self.win._ctx_menu                 # discipline-ok(R5): 菜单的"持有防 GC"就是这个成员，没有对外读取口
        self.assertIsNotNone(menu, "右键没弹出菜单")
        return menu

    def row(self, menu, text):
        for a in menu.actions():
            if a.text() == text:
                return a
        self.fail("菜单里没有「%s」这一行，got %r" % (text, [a.text() for a in menu.actions()]))


class TestRefreshMenu(_WindowCase):
    def test_menu_refresh_reaches_both_workers_and_updates_the_window(self):
        from contextlib import ExitStack
        price = ["101.00"]
        requests = []
        workers = []

        def fetch_quote(code):
            requests.append(("quote", code, threading.current_thread()))
            return {"name": "贵州茅台", "code": "600519", "price": price[0],
                    "pct": "1.00", "change": "1.00", "pre_close": "100.00",
                    "high": "103.00", "low": "99.00"}

        def fetch_minute(code):
            requests.append(("minute", code, threading.current_thread()))
            return [("09:30", float(price[0]), 100.0)]

        def displayed():
            QApplication.processEvents()
            return (self.win.quote is not None
                    and self.win.quote.get("price") == price[0]
                    and self.win.minute_points
                    and self.win.minute_points[-1][1] == float(price[0]))

        with ExitStack() as stack:
            stack.enter_context(patch.object(m, "fetch_quote", side_effect=fetch_quote))
            stack.enter_context(patch.object(m, "fetch_minute", side_effect=fetch_minute))
            stack.enter_context(patch.object(self.win, "_poll_interval", return_value=3600.0))
            stack.enter_context(patch.object(self.win, "_is_trading_time", return_value=False))
            # Scheduling is off-market, but quote freshness must not follow wall-clock lunch.
            stack.enter_context(patch.object(m, "market_status", return_value=None))
            self.win.running = True
            self.win._wake.clear()  # discipline-ok(R5): reactivate only the isolated fixture's workers
            for name in ("_resched", "_quote_resched", "_minute_resched"):
                getattr(self.win, name).clear()
            try:
                for slot, method in (("_quote_thread", "_quote_loop"),
                                     ("_minute_thread", "_minute_loop")):
                    worker = threading.Thread(target=getattr(self.win, method))
                    setattr(self.win, slot, worker)
                    workers.append(worker)
                    worker.start()
                self.assertTrue(self.wait_for(displayed), "Initial data did not reach Qt")
                requests.clear()
                price[0] = "102.00"
                menu = self.open_menu()
                self.row(menu, "刷新行情").trigger()
                self.assertTrue(self.wait_for(displayed), "Refresh never reached the window")
                self.assertCountEqual([(kind, code) for kind, code, _ in requests],
                                      [("quote", "sh600519"), ("minute", "sh600519")])
                self.assertTrue(all(worker is not threading.current_thread()
                                    for _, _, worker in requests))
            finally:
                self.win._shutdown_threads()  # discipline-ok(R5): lifecycle entry ensures worker cleanup even when an assertion fails
                for worker in workers:
                    worker.join(2.0)
                self.assertFalse(any(worker.is_alive() for worker in workers))

    def test_context_refresh_dispatches_once(self):
        calls = []

        def refresh():
            calls.append("refresh")

        with patch.object(self.win, "_force_fetch", new=refresh):
            menu = self.open_menu()
            action = self.row(menu, "刷新行情")
            self.assertTrue(action.isEnabled())
            self.assertFalse(action.icon().isNull())
            self.assertNotIn("立即刷新", [row.text() for row in menu.actions()])
            action.trigger()
            self.assertEqual(calls, ["refresh"])

    def test_native_refresh_has_matching_title_shortcut_and_callback(self):
        from sf_macos_menu import install_window_actions
        from PyQt6.QtGui import QKeySequence
        calls = []

        def refresh():
            calls.append("refresh")

        with patch.object(self.win, "_force_fetch", new=refresh):
            actions, bar = install_window_actions(self.win)
            self.assertIsNone(bar)
            action = actions["refresh"]
            self.assertEqual(action.text(), "刷新行情")
            self.assertEqual(action.shortcut(), QKeySequence("Ctrl+R"))
            action.trigger()
            self.assertEqual(calls, ["refresh"])


class TestMenuIconSheet(SFCase):
    """出图层：五张图渲染得出来、两两不同、出图口径成对（§6 第 7 条：走 SFCase）。"""

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def test_all_five_render_and_are_pairwise_different(self):
        imgs = sf_icons.menu_images()
        self.assertEqual(set(imgs), set(sf_icons.MENU_ICON_KEYS))
        for key in sf_icons.MENU_ICON_KEYS:
            self.assertIsNotNone(imgs[key],
                                 "%s 没渲染出来（只有 QtSvg 缺失才会 None，本机不该发生）" % key)
        sigs = {k: _signature(QIcon(QPixmap.fromImage(v))) for k, v in imgs.items()}
        self.assertEqual(len(set(sigs.values())), len(sigs),
                         "五枚图标里有两枚画出了同一个东西：%r" % (sigs,))

    def test_shipped_images_carry_the_device_ratio(self):
        """边长与 DPR 必须成对：只放大不标 DPR，Retina 上就是一张被上采样的糊图（v1.4.59 S8）。"""
        expect_px = int(round(MENU_ICON_PT * MENU_ICON_DEVICE_RATIO))
        for key in sf_icons.MENU_ICON_KEYS:
            img = sf_icons.menu_images()[key]
            self.assertEqual(img.width(), expect_px,
                             "%s 出图 %d 设备像素，口径是 %d" % (key, img.width(), expect_px))
            self.assertEqual(img.devicePixelRatio(), MENU_ICON_DEVICE_RATIO,
                             "%s 没标 DPR：Qt 会把它当 %dpt 画，比图标列大 %s 倍"
                             % (key, img.width(), img.width() / MENU_ICON_PT))

    def test_missing_qtsvg_yields_no_icon_instead_of_an_empty_slot(self):
        """渲染不出来时必须返回 None，让调用方**跳过** setIcon ——
        贴一枚空 QIcon 会让菜单留出一个空图标位，比没图标更怪。"""
        with patch.object(sf_icons, "svg_pixmap", return_value=None):
            sf_icons._menu_images_cache = None    # discipline-ok(R5): 缓存没有对外失效口，测试必须自己清，否则会带着上一例的真图
            try:
                for key in sf_icons.MENU_ICON_KEYS:
                    self.assertIsNone(sf_icons.menu_icon(key), "%s 应当取不到图" % key)
            finally:
                sf_icons._menu_images_cache = None    # discipline-ok(R5): 同上，把缓存交还给后面的用例


class TestMenuRowsCarryIcons(_WindowCase):
    """行为层：哪几行有图标、眼睛随口径换、取不到图时退回老形状。"""

    def test_four_rows_carry_an_icon_and_the_version_row_does_not(self):
        menu = self.open_menu()
        by_text = {a.text(): a for a in menu.actions() if not a.isSeparator()}
        for text in ROWS_WITH_ICON:
            self.assertIn(text, by_text, "菜单少了「%s」这一行" % text)
            self.assertFalse(by_text[text].icon().isNull(),
                             "「%s」行首没贴图标（用户这轮要的就是它）" % text)
        version = [a for t, a in by_text.items() if t.startswith("v") and not a.isEnabled()]
        self.assertTrue(version, "菜单里找不到禁用的版本行")
        self.assertTrue(version[0].icon().isNull(),
                        "版本行是点不动的灰字，不该有图标：%r" % (version[0].icon(),))

    def test_diagnostics_row_is_now_called_导出诊断包(self):
        texts = [a.text() for a in self.open_menu().actions() if not a.isSeparator()]
        self.assertIn("导出诊断包", texts, "文案没改：%r" % (texts,))
        self.assertNotIn("导出诊断信息", texts,
                         "旧名字还在菜单里，跟桌面上的 `Skyrocket-诊断-*.zip` 对不上：%r" % (texts,))

    def test_eye_icon_is_driven_by_the_same_predicate_as_the_check(self):
        """图标必须取自 `_face_guard_switch_on()` —— 与勾号、与点击目标同一个函数。

        这条是整个改动里唯一会"说谎"的地方：口径一分叉，用户就会看到
        睁着眼却点成"再开一次"（v1.4.67 的原发缺陷换了个载体回来）。
        """
        menu = self.open_menu()
        act = self.row(menu, "自动隐藏窗口")
        for on, key in ((True, EYE_ON), (False, EYE_OFF)):
            with patch.object(self.win, "_face_guard_switch_on", return_value=on):
                self.win._refresh_face_guard_menu_check()   # discipline-ok(R5): 300ms 定时器的回调，等真表会给用例引入抖动
            self.assertEqual(act.isChecked(), on, "勾号没跟着口径走（on=%s）" % on)
            self.assertEqual(_signature(act.icon()), _signature(_icon_for(key)),
                             "on=%s 时图标不是 %s 那一枚" % (on, key))

    def test_the_two_eye_states_are_not_the_same_pixmap(self):
        """两态得真的画得不一样 —— 否则"随状态换图标"这句话是空的。"""
        menu = self.open_menu()
        act = self.row(menu, "自动隐藏窗口")
        with patch.object(self.win, "_face_guard_switch_on", return_value=True):
            self.win._refresh_face_guard_menu_check()       # discipline-ok(R5): 同上，直接驱动刷新回调
        on_sig = _signature(act.icon())
        with patch.object(self.win, "_face_guard_switch_on", return_value=False):
            self.win._refresh_face_guard_menu_check()       # discipline-ok(R5): 同上
        self.assertNotEqual(on_sig, _signature(act.icon()),
                            "开与关画的是同一张图：状态换了界面看不出来")

    def test_without_qtsvg_the_menu_keeps_the_checkmark_it_used_to_have(self):
        """降级方向：取不到图时**不放图标**，那一行回到"无图标 + 勾号"的老形状，
        开关状态照样看得见（不许变成"两态皆盲"）。"""
        empty = {k: None for k in sf_icons.MENU_ICON_KEYS}
        with patch.object(sf_icons, "menu_images", return_value=empty):
            menu = self.open_menu()
            for text in ROWS_WITH_ICON:
                self.assertTrue(self.row(menu, text).icon().isNull(),
                                "拿不到图还硬贴，界面上会是一格空白：%s" % text)
            act = self.row(menu, "自动隐藏窗口")
            self.assertTrue(act.isCheckable(), "降级路径把可勾选也一起丢了")
            with patch.object(self.win, "_face_guard_switch_on", return_value=True):
                self.win._refresh_face_guard_menu_check()   # discipline-ok(R5): 同上
            self.assertTrue(act.isChecked(), "降级时状态仍须由勾号表达")


class TestMenuIconPixels(_WindowCase):
    """像素层：真帧里图标落在文字左边，且两态不同；判据本身可失败。"""

    def _frame(self):
        menu = self.open_menu()
        return menu, menu.grab().toImage()

    def _icon_text_heights(self, menu, img, text):
        rect = menu.actionGeometry(self.row(menu, text))
        runs = _row_runs(img, rect)
        self.assertGreaterEqual(len(runs), 2, "图标与文字没有分开：%s" % text)
        heights = []
        for left, right in (runs[0], (runs[1][0], img.width() - 1)):
            ys = [y for y in range(rect.top(), rect.bottom() + 1)
                  if any(abs(img.pixelColor(x, y).red() - MENU_BG.red())
                         + abs(img.pixelColor(x, y).green() - MENU_BG.green())
                         + abs(img.pixelColor(x, y).blue() - MENU_BG.blue()) > 60
                         for x in range(left, right + 1))]
            self.assertTrue(ys, "图标或文字没有墨迹：%s" % text)
            heights.append(max(ys) - min(ys) + 1)
        return heights

    def test_icons_are_slightly_shorter_than_the_text_in_both_eye_states(self):
        for on in (False, True):
            with patch.object(self.win, "_face_guard_switch_on", return_value=on):
                menu, img = self._frame()
            for text in ROWS_WITH_ICON:
                with self.subTest(text=text, on=on):
                    icon_h, text_h = self._icon_text_heights(menu, img, text)
                    self.assertLess(icon_h, text_h, "图标应比文字略矮：%s" % text)
                    self.assertGreaterEqual(icon_h / text_h, 0.7,
                                            "图标缩得过小：%s" % text)

    def test_size_judge_rejects_the_original_unscaled_icons(self):
        originals = {key: sf_icons.svg_pixmap(svg, int(round(
            MENU_ICON_PT * MENU_ICON_DEVICE_RATIO)))
            for key, svg in sf_icons._MENU_ICON_SVG.items()}  # discipline-ok(R5): 原始内嵌素材没有公开读取口，反向对照须用同一素材
        for img in originals.values():
            img.setDevicePixelRatio(MENU_ICON_DEVICE_RATIO)
        with patch.object(sf_icons, "menu_images", return_value=originals):
            menu, img = self._frame()
        for text in ROWS_WITH_ICON:
            icon_h, text_h = self._icon_text_heights(menu, img, text)
            self.assertGreaterEqual(icon_h, text_h,
                                    "原尺寸未被判为过高，尺寸判据失效：%s" % text)

    def test_icons_are_painted_left_of_the_text(self):
        menu, img = self._frame()
        band_right = _icon_band_right_edge(menu, img)
        for text in ROWS_WITH_ICON:
            runs = _icon_ink_runs(menu, img, self.row(menu, text), band_right)
            self.assertTrue(runs, "「%s」在图标列（x<%d）里一个像素都没画" % (text, band_right))
            first = runs[0]
            self.assertGreaterEqual(first[1] - first[0] + 1, MENU_ICON_PT - 6,
                                    "「%s」图标列里的墨只有 %d px，不是一枚 %dpt 的图"
                                    % (text, first[1] - first[0] + 1, MENU_ICON_PT))
            self.assertLess(first[1], band_right, "「%s」图标画进了文字区" % text)

    def _eye_band(self, menu, band_right):
        act = self.row(menu, "自动隐藏窗口")
        return _icon_band_ink(menu.grab().toImage(), menu.actionGeometry(act), band_right)

    def test_icons_have_breathing_room_on_both_sides(self):
        """图标不许贴着行首：左边距与"图标到文字"的间距都要有，且别差太多。

        负责人看图的原话是"距离左侧边缘太近了"。量法全取相对关系
        （§6 第 6 条：裸像素数换台机器就只是在量别的东西）——
        左边距取"图标第一个墨列 − 行首"，右间距取"文字起点 − 图标最后那个墨列"。
        改坏这条的种法：把样式表里 `QMenu::icon { left }` 删掉，左边距回到 2px，
        本条必须当场判红（实测已验）。
        """
        menu, img = self._frame()
        band_right = _icon_band_right_edge(menu, img)
        for text in ROWS_WITH_ICON:
            act = self.row(menu, text)
            rect = menu.actionGeometry(act)
            runs = _row_runs(img, rect)
            self.assertGreaterEqual(len(runs), 2,
                                    "「%s」这一行只量到一段墨（%r），图标与文字没分开"
                                    % (text, runs))
            icon_left, icon_right = runs[0]
            text_left = runs[1][0]
            lead = icon_left - rect.left()
            gap = text_left - icon_right - 1
            self.assertGreaterEqual(lead, 6,
                                    "「%s」图标离行首只有 %dpx（高亮胶囊左边只剩这点，"
                                    "悬停时图压在蓝边上）" % (text, lead))
            self.assertGreaterEqual(gap, 6,
                                    "「%s」图标与文字只剩 %dpx，快贴上去了" % (text, gap))
            self.assertLessEqual(abs(lead - gap), 8,
                                 "「%s」左边距 %d 与字距 %d 差太多，看着就是没对齐"
                                 % (text, lead, gap))
            self.assertLess(icon_right, band_right, "「%s」图标画进了文字区" % text)

    def test_the_two_eye_states_paint_different_pixels_in_the_same_band(self):
        """开关两态在图标列里必须画出**不同的东西**（不是同一张图换个说法）。"""
        menu, img = self._frame()
        band_right = _icon_band_right_edge(menu, img)
        shots = {}
        for on in (True, False):
            with patch.object(self.win, "_face_guard_switch_on", return_value=on):
                self.win._refresh_face_guard_menu_check()   # discipline-ok(R5): 同上
            QApplication.processEvents()
            shots[on] = self._eye_band(menu, band_right)
        diff = _ink_difference(shots[True], shots[False])
        area = len(shots[True])
        self.assertGreater(diff, area * 0.02,
                           "开/关两态的图标列只有 %d/%d 个点不同 —— 换的不是两张图"
                           % (diff, area))
        # 同态复量必须为 0：不然上面那条"有差异"可能只是渲染噪声，而不是状态换了。
        with patch.object(self.win, "_face_guard_switch_on", return_value=True):
            self.win._refresh_face_guard_menu_check()       # discipline-ok(R5): 同上
        QApplication.processEvents()
        self.assertEqual(_ink_difference(shots[True], self._eye_band(menu, band_right)), 0,
                         "同一个状态量两次都对不上，这条判据量的是噪声不是状态")

    def test_the_pixel_judge_can_fail(self):
        """反向对照：同一套量法在**没贴图标**的那一帧上必须报"图标列是空的"。

        没有这条，上面的断言可能在尺子取到空输入时无条件成立 ——
        与 test_discipline_scan 的 min_files、coverage_gate 的"空分档"同族。
        """
        empty = {k: None for k in sf_icons.MENU_ICON_KEYS}
        with patch.object(sf_icons, "menu_images", return_value=empty):
            menu, img = self._frame()
            band_right = _icon_band_right_edge(menu, img)
            silent = [t for t in ROWS_WITH_ICON
                      if not _icon_ink_runs(menu, img, self.row(menu, t), band_right)]
            self.assertEqual(len(silent), len(ROWS_WITH_ICON),
                             "没图标的帧里图标列仍然「有墨」（%r）—— 上面那两条判据是假门"
                             % (silent,))


if __name__ == "__main__":
    unittest.main(verbosity=2)
