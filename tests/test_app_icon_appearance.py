# -*- coding: utf-8 -*-
"""应用图标随系统浅/深色外观切换（2026-10-23，用户要求）。

要的效果：系统是浅色外观，图标用浅灰色块底；系统是深色外观，图标用深色色块底。
包体 icon.icns 是固定的一张（浅灰底），运行时的 Dock 图标与窗口图标按外观换。

判据分三层，每层都配反向对照：
  1. 纯函数层：深色 markup 只换两种颜色、形状不动；外观枚举到档位的映射；
     取不到外观时一律按浅色，且不向上抛。
  2. 信号层：连不上 / 没有应用对象时如实返回 False 并留痕。
  3. 窗口层：两张图到位后按外观贴；外观变了换图；外观没变不重复设置；
     深色那张没出好就沿用浅色；回调里炸了只留日志不外抛。

窗口层看的是公开输出 —— QApplication 与窗口当前的图标，取样里面画的底色，
不断言源码写法，也不读私有成员（下面少数几处必须读的写了理由）。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtCore import QSize, Qt  # noqa: E402
from PyQt6.QtGui import QGuiApplication, QIcon, QPixmap  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

import sf_icons  # noqa: E402
import sf_quote  # noqa: E402
import stock_float_window as m  # noqa: E402

try:
    from _base import SFCase                                   # noqa: E402
except ImportError:                                            # 以包形式被 discover 导入时
    from tests._base import SFCase                             # noqa: E402


LIGHT_PLATE, DARK_PLATE = "#e8ebf0", "#12151d"     # 定稿底色；测试自己写一份，不引用被测常量
ICON_PROBE_PX = 128                                 # 窗口层用的小图边长，够取样又快
PLATE_SAMPLE_X = 0.18                               # 色块左侧中段，在标志左边、避开圆角


def _plate_of(icon):
    """QIcon 里画的底色：取 128 像素档左侧中段一点。"""
    img = icon.pixmap(QSize(ICON_PROBE_PX, ICON_PROBE_PX)).toImage()
    return img.pixelColor(int(img.width() * PLATE_SAMPLE_X), img.height() // 2).name()


def _icons_pair():
    return (sf_icons.render_app_icon("light", ICON_PROBE_PX),
            sf_icons.render_app_icon("dark", ICON_PROBE_PX))


# --------------------------------------------------------------------------
# 1. 纯函数层
# --------------------------------------------------------------------------

class TestBrandLogoMarkup(SFCase):
    def test_light_markup_is_the_approved_one_untouched(self):
        self.assertEqual(sf_icons.brand_logo_svg("light"), sf_icons.BRAND_LOGO_SVG)
        self.assertEqual(sf_icons.brand_logo_svg(), sf_icons.BRAND_LOGO_SVG)

    def test_dark_markup_swaps_exactly_the_two_figure_colours(self):
        dark = sf_icons.brand_logo_svg("dark")
        self.assertIn("#fe0118", dark)
        self.assertIn("#f2f4f8", dark)
        self.assertNotIn("#f40000", dark, "浅色档的红还留在深色 markup 里")
        self.assertNotIn("#14181f", dark, "浅色档的墨还留在深色 markup 里")

    def test_dark_markup_keeps_every_path_exactly(self):
        """形状一个点都不动：把两处颜色换回去，必须逐字等于浅色 markup。"""
        dark = sf_icons.brand_logo_svg("dark")
        restored = dark.replace("#fe0118", "#f40000").replace("#f2f4f8", "#14181f")
        self.assertEqual(restored, sf_icons.BRAND_LOGO_SVG)

    def test_unknown_mode_is_treated_as_light(self):
        self.assertEqual(sf_icons.brand_logo_svg("sepia"), sf_icons.BRAND_LOGO_SVG)
        self.assertEqual(sf_icons.brand_logo_svg(None), sf_icons.BRAND_LOGO_SVG)


class TestModeFromScheme(SFCase):
    def test_dark_scheme_maps_to_dark(self):
        self.assertEqual(sf_icons.icon_mode_from_scheme(Qt.ColorScheme.Dark), "dark")

    def test_light_and_unknown_schemes_map_to_light(self):
        """Unknown 是离屏平台、旧系统读不到外观时的值，必须落在浅色。"""
        self.assertEqual(sf_icons.icon_mode_from_scheme(Qt.ColorScheme.Light), "light")
        self.assertEqual(sf_icons.icon_mode_from_scheme(Qt.ColorScheme.Unknown), "light")


class TestSystemIconMode(SFCase):
    @staticmethod
    def _fake_app(scheme=None, boom=None):
        hints = mock.Mock()
        if boom is not None:
            hints.colorScheme.side_effect = boom
        else:
            hints.colorScheme.return_value = scheme
        return mock.Mock(instance=mock.Mock(return_value=object()),
                         styleHints=mock.Mock(return_value=hints))

    def test_reads_the_scheme_of_the_running_application(self):
        with mock.patch.object(sf_icons, "QGuiApplication", self._fake_app(Qt.ColorScheme.Dark)):
            self.assertEqual(sf_icons.system_icon_mode(), "dark")
        with mock.patch.object(sf_icons, "QGuiApplication", self._fake_app(Qt.ColorScheme.Light)):
            self.assertEqual(sf_icons.system_icon_mode(), "light")

    def test_no_application_object_means_light(self):
        with mock.patch.object(sf_icons, "QGuiApplication", mock.Mock(instance=mock.Mock(return_value=None))):
            self.assertEqual(sf_icons.system_icon_mode(), "light")

    def test_missing_style_hints_means_light(self):
        """有应用对象但取不到样式提示（存根允许 None）：按浅色，不抛。"""
        fake = mock.Mock(instance=mock.Mock(return_value=object()), styleHints=mock.Mock(return_value=None))
        with mock.patch.object(sf_icons, "QGuiApplication", fake):
            self.assertEqual(sf_icons.system_icon_mode(), "light")

    def test_a_failing_read_means_light_and_leaves_a_trace(self):
        with mock.patch.object(sf_icons, "QGuiApplication", self._fake_app(boom=RuntimeError("no hints"))), \
                mock.patch.object(sf_icons, "log_swallow") as swallow:
            self.assertEqual(sf_icons.system_icon_mode(), "light")
        self.assertEqual(swallow.call_count, 1, "读外观失败必须留一条 SWALLOWED，不能静默")


# --------------------------------------------------------------------------
# 2. 信号层
# --------------------------------------------------------------------------

class TestConnectAppearanceChanged(SFCase):
    def test_no_application_object_returns_false(self):
        with mock.patch.object(sf_icons, "QGuiApplication", mock.Mock(instance=mock.Mock(return_value=None))):
            self.assertFalse(sf_icons.connect_appearance_changed(lambda *_: None))

    def test_missing_style_hints_returns_false(self):
        fake = mock.Mock(instance=mock.Mock(return_value=object()), styleHints=mock.Mock(return_value=None))
        with mock.patch.object(sf_icons, "QGuiApplication", fake):
            self.assertFalse(sf_icons.connect_appearance_changed(lambda *_: None))

    def test_connects_the_slot_to_the_scheme_changed_signal(self):
        signal = mock.Mock()
        fake = mock.Mock(instance=mock.Mock(return_value=object()),
                         styleHints=mock.Mock(return_value=mock.Mock(colorSchemeChanged=signal)))
        slot = mock.Mock()
        with mock.patch.object(sf_icons, "QGuiApplication", fake):
            self.assertTrue(sf_icons.connect_appearance_changed(slot))
        signal.connect.assert_called_once_with(slot)

    def test_a_failing_connect_returns_false_and_leaves_a_trace(self):
        signal = mock.Mock()
        signal.connect.side_effect = RuntimeError("no such signal")
        fake = mock.Mock(instance=mock.Mock(return_value=object()),
                         styleHints=mock.Mock(return_value=mock.Mock(colorSchemeChanged=signal)))
        with mock.patch.object(sf_icons, "QGuiApplication", fake), \
                mock.patch.object(sf_icons, "log_swallow") as swallow:
            self.assertFalse(sf_icons.connect_appearance_changed(lambda *_: None))
        self.assertEqual(swallow.call_count, 1)


# --------------------------------------------------------------------------
# 3. 窗口层
# --------------------------------------------------------------------------

class _WindowCase(SFCase):
    """造一个真 FloatWindow，图标后台线程关掉，由用例自己往队列里放图。

    patch 清单照 tests/test_menu_row_icons._WindowCase 抄全（AGENTS §6 第 14 条）。
    QApplication 的窗口图标是进程级状态，用例前后各保存/还原一次。
    """

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])    # 类上持有引用，否则被 GC

    def setUp(self):
        super().setUp()
        saved_icon = QApplication.windowIcon()
        self.addCleanup(QApplication.setWindowIcon, saved_icon)
        self._drain()
        self.addCleanup(self._drain)
        self.track_patch(mock.patch.object(
            m, "load_config", return_value=("sh600519", 100, 100, 200, "translucent")))
        self.track_patch(mock.patch.object(m, "save_config", lambda *a, **k: True))
        self.track_patch(mock.patch.object(sf_quote, "http_get", lambda *a, **k: ""))
        self.track_patch(mock.patch.object(m, "start_icon_worker", lambda *a, **k: None))
        self.track_patch(mock.patch.object(m, "show_dock", lambda *a, **k: None))   # 不动真实 Dock 策略
        self.track_patch(mock.patch.object(m.FloatWindow, "_force_top", lambda self: None))
        self.track_patch(mock.patch.object(m.FloatWindow, "_inject_glass", lambda self: None))
        self.track_patch(mock.patch.object(m.FloatWindow, "_setup_reopen_handler", lambda self: None))
        self.track_patch(mock.patch.object(m.FloatWindow, "raise_", lambda self: None))
        self.track_patch(mock.patch.object(m.FloatWindow, "activateWindow", lambda self: None))
        self.track_patch(mock.patch.object(m.FloatWindow, "_quote_loop_wrapper", lambda self: None))
        self.track_patch(mock.patch.object(m.FloatWindow, "_minute_loop_wrapper", lambda self: None))
        self.win = self.track_widget(m.FloatWindow())
        self.win.running = False
        self.win._wake.set()                      # discipline-ok(R5): 停线程的公开开关是 running，_wake 是配套唤醒位，没有对外读法
        self.win._resched.set()                   # discipline-ok(R5): 同上，重排唤醒位没有对外接口
        self.win.timer.stop()                     # discipline-ok(R5): 常驻定时器没有对外停止口

    def tearDown(self):
        # §6 第 11 条：关窗必须排在 patch 还活着的时候，否则 closeEvent 会走真实 save_config
        try:
            self.win._exit_persisted = True       # discipline-ok(R5): 落盘幂等标记没有对外接口
            self.win.close()
        finally:
            self.win.running = False
            for t in (self.win._minute_thread, self.win._quote_thread):   # discipline-ok(R5): 后台线程没有对外 join 口
                t.join(2.0)
            super().tearDown()

    @staticmethod
    def _drain():
        try:
            while True:
                sf_icons._ICON_QUEUE.get_nowait()   # discipline-ok(R5): 图标队列没有对外读取口
        except Exception:
            pass

    def deliver(self, light=True, dark=True):
        """模拟后台把图标投到位，再走生产入口 _apply_icons。"""
        li, di = _icons_pair()
        if light:
            sf_icons._ICON_QUEUE.put(("app_icon", li))   # discipline-ok(R5): 模拟后台线程投递，队列没有对外写入口
        if dark:
            sf_icons._ICON_QUEUE.put(("app_icon_dark", di))   # discipline-ok(R5): 同上
        self.win._apply_icons()   # discipline-ok(R5): 生产入口，由 _poll 调用，没有对外名字


class TestWindowFollowsSystemAppearance(_WindowCase):
    def test_light_system_shows_the_light_plate_everywhere(self):
        with mock.patch.object(m, "system_icon_mode", return_value="light"):
            self.deliver()
        self.assertEqual(_plate_of(self.win.windowIcon()), LIGHT_PLATE)
        self.assertEqual(_plate_of(QApplication.windowIcon()), LIGHT_PLATE, "Dock 用的是应用级图标")

    def test_dark_system_shows_the_dark_plate_everywhere(self):
        with mock.patch.object(m, "system_icon_mode", return_value="dark"):
            self.deliver()
        self.assertEqual(_plate_of(self.win.windowIcon()), DARK_PLATE)
        self.assertEqual(_plate_of(QApplication.windowIcon()), DARK_PLATE)

    def test_switching_appearance_swaps_the_icon_without_new_images(self):
        mode = {"now": "light"}
        with mock.patch.object(m, "system_icon_mode", side_effect=lambda: mode["now"]):
            self.deliver()
            self.assertEqual(_plate_of(QApplication.windowIcon()), LIGHT_PLATE)
            mode["now"] = "dark"
            self.win._on_appearance_changed()     # discipline-ok(R5): 信号槽，没有对外入口；真信号那条在下一个用例
            self.assertEqual(_plate_of(QApplication.windowIcon()), DARK_PLATE)
            self.assertEqual(_plate_of(self.win.windowIcon()), DARK_PLATE)
            mode["now"] = "light"
            self.win._on_appearance_changed()     # discipline-ok(R5): 同上
            self.assertEqual(_plate_of(QApplication.windowIcon()), LIGHT_PLATE)

    def test_the_real_scheme_signal_reaches_the_window(self):
        """端到端：窗口构造时连的是真信号。发一次 Dark，图标必须换成深色底。"""
        with mock.patch.object(m, "system_icon_mode", return_value="light"):
            self.deliver()
        self.assertEqual(_plate_of(QApplication.windowIcon()), LIGHT_PLATE)
        with mock.patch.object(m, "system_icon_mode", return_value="dark"):
            QGuiApplication.styleHints().colorSchemeChanged.emit(Qt.ColorScheme.Dark)
            self.assertEqual(_plate_of(QApplication.windowIcon()), DARK_PLATE,
                             "窗口没有连上系统外观切换的信号：深色外观下 Dock 图标不会换")

    def test_missing_dark_icon_keeps_the_light_one_even_in_dark_mode(self):
        """深色那张后台没出好（以 None 占位 = 没入队）：深色外观下沿用浅色，不能没有图标。"""
        with mock.patch.object(m, "system_icon_mode", return_value="dark"):
            self.deliver(dark=False)
        self.assertEqual(_plate_of(QApplication.windowIcon()), LIGHT_PLATE)

    def test_dark_arriving_later_takes_over_in_dark_mode(self):
        """两张不是同一拍到的：浅色先到先贴，深色后到，深色外观下必须换过去。"""
        with mock.patch.object(m, "system_icon_mode", return_value="dark"):
            self.deliver(dark=False)
            self.assertEqual(_plate_of(QApplication.windowIcon()), LIGHT_PLATE)
            self.deliver(light=False)
            self.assertEqual(_plate_of(QApplication.windowIcon()), DARK_PLATE)

    def test_no_icon_arrived_means_no_icon_is_set(self):
        """队列里没有应用图标时不碰 Dock/窗口图标：不能把别处设好的图标冲成空的。"""
        pm = QPixmap(ICON_PROBE_PX, ICON_PROBE_PX)
        pm.fill(Qt.GlobalColor.magenta)
        QApplication.setWindowIcon(QIcon(pm))
        before = QApplication.windowIcon().cacheKey()
        with mock.patch.object(m, "system_icon_mode", return_value="dark"):
            self.win._apply_icons()   # discipline-ok(R5): 同上
        self.assertEqual(QApplication.windowIcon().cacheKey(), before)

    def test_unchanged_appearance_does_not_set_the_icon_again(self):
        """外观没变就别重复设置：每设一次，Dock 都会重绘一次图标。"""
        with mock.patch.object(m, "system_icon_mode", return_value="dark"):
            self.deliver()
            with mock.patch.object(m.QApplication, "setWindowIcon") as set_app_icon:
                self.win._on_appearance_changed()     # discipline-ok(R5): 信号槽，没有对外入口
                self.win._on_appearance_changed()     # discipline-ok(R5): 同上
            self.assertEqual(set_app_icon.call_count, 0)

    def test_a_failing_sync_is_logged_not_raised(self):
        """回调在 Qt 信号里跑，抛出去会变成未捕获异常；必须留 SWALLOWED 并吞掉。"""
        with mock.patch.object(self.win, "_sync_app_icon", side_effect=RuntimeError("boom")), \
                mock.patch.object(m, "log_swallow") as swallow:
            self.win._on_appearance_changed()     # discipline-ok(R5): 信号槽，没有对外入口
        self.assertEqual(swallow.call_count, 1)


if __name__ == "__main__":
    unittest.main()
