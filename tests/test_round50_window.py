# -*- coding: utf-8 -*-
"""2026-11-02 第 50 轮走查（w3-window 批）：主窗 / 渲染 / 守卫侧缺陷的判据。

每条用例都对应走查编号（F-X-NN），且都在修复前实测为红。
构造 FloatWindow 一律走本文件的 `_Round50Win` 夹具（带完整隔离桩：配置路径、
行情网络、图标线程、常驻循环全部替掉），绝不裸 `m.FloatWindow()` ——
那会拉起正式版（AGENTS §6 第 14 条）。
"""

import os
import shutil
import sys
import tempfile
import unittest
from collections import Counter
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtGui import QColor, QImage, QPainter
from PyQt6.QtWidgets import QApplication

try:
    from _base import SFCase                       # noqa: E402
except ImportError:
    from tests._base import SFCase                 # noqa: E402

import sf_config
import sf_quote
import sf_ui_render as R
import stock_float_window as m


def good_quote(**kw):
    q = {"name": "贵州茅台", "code": "600519", "price": "1510.00",
         "pct": "1.34", "change": "20.00", "pre_close": "1490.00",
         "high": "1520.00", "low": "1480.00",
         "volume": "12345", "limit_up": "1639.00", "limit_down": "1341.00"}
    q.update(kw)
    return q


class _Round50Win(SFCase):
    """带完整隔离桩的主窗夹具（与 test_power_v1410._WinCase 同口径，另加临时配置路径）。"""

    def setUp(self):
        super().setUp()
        self.app = QApplication.instance() or QApplication([])
        R.load_bundled_fonts()
        self.tmp = tempfile.mkdtemp(prefix="sf_round50_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg = os.path.join(self.tmp, "config.json")
        self.bak = os.path.join(self.tmp, "config.backup.json")

    def make_win(self, show=True, load_config=None):
        for p in (
            mock.patch.object(m, "load_config", return_value=load_config
                              or ("sh600519", 100, 100, 224, "glass")),
            mock.patch.object(m, "save_config", lambda *a, **k: True),
            mock.patch.object(sf_quote, "http_get", lambda *a, **k: ""),
            mock.patch.object(m, "start_icon_worker", lambda: None),
            mock.patch.object(sf_config, "CONFIG_PATH", self.cfg),
            mock.patch.object(sf_config, "CONFIG_BACKUP_PATH", self.bak),
            mock.patch.object(m.FloatWindow, "_force_top", lambda self: None),
            mock.patch.object(m.FloatWindow, "_inject_glass", lambda self: None),
            mock.patch.object(m.FloatWindow, "_setup_reopen_handler", lambda self: None),
            mock.patch.object(m.FloatWindow, "raise_", lambda self: None),
            mock.patch.object(m.FloatWindow, "activateWindow", lambda self: None),
            # 常驻循环整体停掉：后台线程不许在断言期间自己改状态
            mock.patch.object(m.FloatWindow, "_quote_loop_wrapper", lambda self: None),
            mock.patch.object(m.FloatWindow, "_minute_loop_wrapper", lambda self: None),
        ):
            self.track_patch(p)
        win = self.track_widget(m.FloatWindow())
        win.running = False
        win._wake.set()                              # discipline-ok(R5): 让后台循环线程立刻退出
        win._resched.set()                           # discipline-ok(R5): 同上
        for t in (win.timer, win.top_timer, win._anim_timer, win._flash_timer,   # discipline-ok(R5): 夹具必须停掉全部定时器
                  win._flash_drop_timer, win._rainbow_timer, win._session_timer):  # discipline-ok(R5): 停止其余定时器。
            t.stop()
        if show:
            win.show()
            QApplication.processEvents()
        return win

    def paint(self, win, fn, w=None, h=None):
        """直调 _paint_*（不走 paintEvent：它有兜底会吞异常）。"""
        w = w or win.width()
        h = h or win.height()
        img = QImage(w, h, QImage.Format.Format_ARGB32)
        img.fill(QColor(0, 0, 0, 0))
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        try:
            fn(p, w)
        finally:
            p.end()
        return img

    @staticmethod
    def colors_in(img, box, min_alpha=200):
        """区域内不透明像素的颜色计数（name -> 个数）。"""
        x0, y0, x1, y1 = box
        c = Counter()
        for y in range(y0, min(y1, img.height())):
            for x in range(x0, min(x1, img.width())):
                px = img.pixelColor(x, y)
                if px.alpha() >= min_alpha:
                    c[px.name()] += 1
        return c


class TestMinimizeResetsDropFlash(_Round50Win):
    """F-D-02：下跌闪烁期间点缩小，恢复后价格不许一直是白色。"""

    def test_minimize_during_drop_flash_does_not_leave_the_price_white(self):
        win = self.make_win()
        win.quote = good_quote(change="-30.00", pct="-2.0")           # discipline-ok(R5): 直接塞行情，不走网络
        win._disp_price = 1460.0                                      # discipline-ok(R5): 价格动画的显示值
        win._disp_pct = -2.0                                          # discipline-ok(R5): 同上
        win._anim_decimals = 2                                        # discipline-ok(R5): 同上
        win._pct_unknown = False                                      # discipline-ok(R5): 同上
        win._start_drop_flash()                                       # discipline-ok(R5): 真实下跌闪烁入口，行情触发路径太长
        self.assertTrue(win._flash_drop_timer.isActive(),             # discipline-ok(R5): 前置：闪烁确已启动
                        "前置：下跌闪烁应已启动")
        # 缩到 Dock 的原生副作用打桩（Dock 图标、人脸守卫、缩放动画），其余走真实路径
        with mock.patch.object(m, "show_dock", lambda: None), \
                mock.patch.object(m.FloatWindow, "_sync_face_guard", lambda *a, **k: None), \
                mock.patch.object(m.FloatWindow, "_dock_motion", lambda self: None):
            win.minimize_to_dock()
        # 从 Dock 恢复后的第一帧：价格应是趋势色（跌=绿），不能卡在闪烁相位的白色
        win._minimized = False                                        # discipline-ok(R5): 模拟已恢复，restore 路径有 Dock 动画依赖
        win._chart_expanded = True                                    # discipline-ok(R5): 同上
        img = self.paint(win, win._paint_price, 184, 224)             # discipline-ok(R5): 价格带直调，paintEvent 会吞异常
        colors = self.colors_in(img, (14, 70, 170, 100))
        flash = R.FLASH_DROP_COLOR.name()
        trend = win._trend_color().name()                             # discipline-ok(R5): 与画出来的颜色比对的基准
        self.assertGreater(colors.get(trend, 0), 0,
                           "恢复后价格应按趋势色 %s 绘制，实测 %r" % (trend, colors.most_common(3)))
        self.assertEqual(colors.get(flash, 0), 0,
                         "价格仍是闪烁相位的白色（计时器停了、相位没复位）：%r" % (colors.most_common(3),))


class TestStartupKeepsWindowFullyOnScreen(_Round50Win):
    """F-D-03：启动只钳"完全看不见"的窗口，露出几像素的也要整个拉回可用区。"""

    def test_saved_position_mostly_offscreen_is_pulled_fully_inside(self):
        from PyQt6.QtGui import QGuiApplication
        ag = QGuiApplication.primaryScreen().availableGeometry()
        # 只剩 6px 贴在屏幕右缘：与可用区"相交"，旧的一次性钳制判成"看得见"而不动它
        win = self.make_win(show=False,
                            load_config=("sh600519", ag.right() - 6, 100, 224, "glass"))
        g = win.frameGeometry()
        self.assertGreaterEqual(g.left(), ag.left(), "窗口左缘仍在可用区外：%r" % (g,))
        self.assertLessEqual(g.right(), ag.right(), "窗口右缘仍在可用区外（只露 6px）：%r" % (g,))
        self.assertGreaterEqual(g.top(), ag.top(), "窗口上缘在可用区外：%r" % (g,))
        self.assertLessEqual(g.bottom(), ag.bottom(), "窗口下缘在可用区外：%r" % (g,))


class TestPctColorFollowsTheRoundedText(_Round50Win):
    """F-D-04：文字显示 0.00% 时颜色不许还是红/绿（颜色按取整后显示的值判）。"""

    def test_bottom_rows_tiny_deviation_is_grey_not_red_or_green(self):
        # 昨收 2000：最高只比昨收高 0.05（0.0025%）、最低只低 0.05 —— 文字都是 0.00%
        r1, r2 = R.bottom_rows(good_quote(pre_close="2000.00", high="2000.05", low="1999.95"))
        for row in (r1, r2):
            label, num, color = row[0]
            self.assertEqual(num, "0.00%", "前置：这一档文字应是 0.00%%（%s）" % label)
            self.assertEqual(color.name(), R.FG_DIM.name(),
                             "%s 显示 0.00%% 却画成 %s（红/绿）" % (label, color.name()))

    def test_bottom_rows_real_deviation_keeps_red_and_green(self):
        r1, r2 = R.bottom_rows(good_quote(pre_close="2000.00", high="2100.00", low="1900.00"))
        self.assertEqual(r1[0][2].name(), R.RED.name(), "正向对照：涨 5%% 必须仍是红")
        self.assertEqual(r2[0][2].name(), R.GREEN.name(), "正向对照：跌 5%% 必须仍是绿")

    def test_price_trend_color_is_neutral_when_pct_text_rounds_to_zero(self):
        win = self.make_win()
        for chg, pct in (("0.05", 0.003), ("-0.05", -0.003)):
            win.quote = good_quote(change=chg, pct=str(pct), price="2000.05", pre_close="2000.00")  # discipline-ok(R5): 直接塞行情，不走网络
            win._disp_pct = pct                                       # discipline-ok(R5): 屏幕上显示的涨跌幅就是这个值
            win._pct_unknown = False                                  # discipline-ok(R5): 同上
            win._disp_price = 2000.05                                  # discipline-ok(R5): 绘制价格的动画值
            image = self.paint(win, win._paint_price)                  # discipline-ok(R5): 直接绘制防止吞异常
            colors = self.colors_in(image, (14, 70, 170, 100))
            self.assertGreater(colors.get(R.FG_NAME.name(), 0), 0,
                               "change=%s pct=%s 显示零时价格应为中性色" % (chg, pct))
            self.assertEqual(colors.get((R.RED if pct > 0 else R.GREEN).name(), 0), 0)

    def test_price_trend_color_keeps_red_green_for_real_moves(self):
        win = self.make_win()
        for chg, pct, want in (("20.00", 1.34, R.RED), ("-30.00", -2.0, R.GREEN)):
            win.quote = good_quote(change=chg, pct=str(pct))          # discipline-ok(R5): 同上
            win._disp_pct = pct                                       # discipline-ok(R5): 同上
            win._pct_unknown = False                                  # discipline-ok(R5): 同上
            self.assertEqual(win._trend_color().name(), want.name())  # discipline-ok(R5): 同上

    def test_chart_and_limit_badge_keep_quote_direction_during_zero_animation(self):
        win = self.make_win()
        for change, limit_key, color in (("10.00", "limit_up", R.RED),
                                         ("-10.00", "limit_down", R.GREEN)):
            with self.subTest(change=change):
                win.quote = good_quote(change=change, price="100.00", **{limit_key: "100.00"})
                win._disp_price = 100.0  # discipline-ok(R5): 模拟动画经过零
                win._disp_pct = 0.0  # discipline-ok(R5): 模拟动画经过零
                win._pct_unknown = False  # discipline-ok(R5): 涨跌幅已知
                metrics = win._chart_metrics([("0930", 100.0, 100.0)], None)  # discipline-ok(R5): 图表取色出口
                self.assertEqual(metrics["color"].name(), color.name())
                image = self.paint(win, win._paint_price)  # discipline-ok(R5): 直接绘制防止吞异常
                pixels = self.colors_in(image, (0, 0, image.width(), image.height()))
                self.assertGreater(pixels.get(color.darker(140).name(), 0), 0,
                                   "涨跌停徽标必须保持行情方向色")


class TestZeroPriceFallbackWritesChangeBack(_Round50Win):
    """F-D-05：价格为 0 走分时兜底时，涨跌额也要同步回写，否则价格颜色按旧的 0 判成"平盘"。"""

    def test_fallback_price_below_pre_close_paints_green(self):
        win = self.make_win()
        # 接口给 price=0（休市/盘前/空值）；分时缓存最后一点 97.0 低于昨收 100.0
        win.minute_points = [("0930", 98.0, 98.0), ("1500", 97.0, 97.5)]    # discipline-ok(R5): 兜底只认这份分时缓存，没有公开入口
        data = good_quote(price="0.00", pct="0.00", change="0.00", pre_close="100.00",
                          high="0", low="0", volume="0")
        win._apply_quote(data)                                              # discipline-ok(R5): 行情入主窗的唯一入口，网络线程同款调用
        self.assertEqual(win._trend_color().name(), R.GREEN.name(),         # discipline-ok(R5): 被测的取色入口
                         "兜底价 97 低于昨收 100，价格颜色仍是 %s（涨跌额没回写）"
                         % win._trend_color().name())                       # discipline-ok(R5): 同上
        self.assertLess(float(win.quote["change"]), 0, "兜底后涨跌额应为负：%r" % (win.quote,))

    def test_fallback_without_valid_pre_close_leaves_change_alone(self):
        win = self.make_win()
        win.minute_points = [("1500", 97.0, 97.5)]                          # discipline-ok(R5): 同上
        data = good_quote(price="0.00", pct="0.00", change="0.00", pre_close="",
                          high="0", low="0", volume="0")
        win._apply_quote(data)                                              # discipline-ok(R5): 同上
        self.assertEqual(float(win.quote["change"]), 0.0,
                         "昨收缺失时不许编造涨跌额：%r" % (win.quote,))


class TestFirstScreenSeedRequiresNameAndCode(_Round50Win):
    """F-D-06：首屏快照缺 name/code 时不许预填 —— 顶部名称行读 quote["name"] 会逐帧 KeyError。"""

    def _seeded_window(self, quote):
        snap = {"code": "sh600519", "day": "2026-11-02", "ts": 1.0, "quote": quote,
                "minute": [], "minute_day": ""}
        # 预填默认在测试隔离态被 SF_SEED_FIRST_SCREEN=0 关掉；这里显式打开并把快照换成内存里的这份
        # （load_last_quote 不碰磁盘，所以也不会写用户目录）。
        env = mock.patch.dict(os.environ, {"SF_SEED_FIRST_SCREEN": "1"})
        env.start()
        self.addCleanup(env.stop)
        self.track_patch(mock.patch.object(m, "load_last_quote", return_value=snap))
        return self.make_win(show=False)

    def test_snapshot_without_name_is_not_seeded_and_header_paints(self):
        win = self._seeded_window({"price": "1510.00", "pct": "1.0", "code": "600519"})
        self.assertFalse(win._seeded_from_cache,                     # discipline-ok(R5): 预填标记没有公开读取口
                         "缺 name 的快照被预填了：顶部名称行会逐帧 KeyError")
        img = self.paint(win, win._paint_header)                     # discipline-ok(R5): 直调绘制，paintEvent 会吞异常
        self.assertGreater(img.width(), 0)  # discipline-ok(R6): 验证图像非空，不是固定布局尺寸。

    def test_snapshot_without_code_is_not_seeded(self):
        win = self._seeded_window({"price": "1510.00", "pct": "1.0", "name": "贵州茅台"})
        self.assertFalse(win._seeded_from_cache,                     # discipline-ok(R5): 同上
                         "缺 code 的快照被预填了")

    def test_complete_snapshot_is_still_seeded(self):
        win = self._seeded_window(good_quote())
        self.assertTrue(win._seeded_from_cache,                      # discipline-ok(R5): 同上；正向对照，防"一律不预填"
                        "完整快照必须仍能预填首屏")
        self.paint(win, win._paint_header)                           # discipline-ok(R5): 同上


class TestAppIconPathCanBeRedirected(SFCase):
    """F-D-07：make_app_icon 的落盘路径可经 SF_ICON_PATH 改道；不设时仍写默认路径。

    默认路径一律先换成临时目录里的文件再测 —— 绝不真写用户的 icon.png。
    """

    def setUp(self):
        super().setUp()
        self.ensure_qapp()
        import sf_icons
        self.sf_icons = sf_icons
        self.tmp = tempfile.mkdtemp(prefix="sf_icon_redirect_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.default = os.path.join(self.tmp, "default", "icon.png")
        self.override = os.path.join(self.tmp, "override", "icon.png")
        # 模拟"用户目录里的默认路径"：两处同时换，保持"没被测试直接改写"的形状
        self.track_patch(mock.patch.object(sf_icons, "ICON_PATH", self.default))
        self.track_patch(mock.patch.object(sf_icons, "_DEFAULT_ICON_PATH", self.default))

    def _env(self, **kw):
        base = {k: v for k, v in os.environ.items() if k != "SF_ICON_PATH"}
        base.update(kw)
        self.track_patch(mock.patch.dict(os.environ, base, clear=True))

    def test_env_var_redirects_the_side_effect_file(self):
        self._env(SF_ICON_PATH=self.override)
        self.sf_icons.make_app_icon("light")
        self.assertTrue(os.path.exists(self.override), "SF_ICON_PATH 没有生效：图写到别处了")
        self.assertFalse(os.path.exists(self.default), "设了 SF_ICON_PATH 仍写了默认路径")

    def test_without_env_var_default_path_is_written_as_before(self):
        self._env()
        self.sf_icons.make_app_icon("light")
        self.assertTrue(os.path.exists(self.default), "不设环境变量时必须仍写默认路径（行为不变）")

    def test_direct_module_override_still_beats_the_env_var(self):
        """存量用例（test_coverage_icons_editor）直接改写 sf_icons.ICON_PATH 改道：
        将来测试环境全局设了 SF_ICON_PATH 也不许把它们的路径盖掉。"""
        self._env(SF_ICON_PATH=self.override)
        explicit = os.path.join(self.tmp, "explicit", "icon.png")
        self.track_patch(mock.patch.object(self.sf_icons, "ICON_PATH", explicit))
        self.sf_icons.make_app_icon("light")
        self.assertTrue(os.path.exists(explicit))
        self.assertFalse(os.path.exists(self.override))


if __name__ == "__main__":
    unittest.main()
