# -*- coding: utf-8 -*-
"""v1.2.65 回归：毛玻璃暗化度必须随系统深/浅色外观实时更新。

BUG：旧 `GlassMixin._glass_dim` 把外观探测结果缓存进 `self._glass_mode`，唯一
失效点是 `stock_float_window._on_screen_changed`（只接 primaryScreenChanged /
screenAdded / screenRemoved）。macOS 深/浅色外观切换不发任何屏幕事件 → 缓存
永久陈旧，浅色外观下仍用暗色档暗化度，白字对比度不足。

修复：探测结果不再跨绘制缓存，拆成两个可单测的纯函数
（appearance_mode_from_name / glass_dim_for_mode）+ 一个探测函数
（detect_system_appearance），`_glass_dim` 每次调用实时取值。

运行方式（工程根目录）：
    QT_QPA_PLATFORM=offscreen ./run_tests.sh
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sf_glass
from sf_constants import BG_GLASS_DIM, BG_GLASS_DIM_LIGHT


class _FakeAppearance(object):
    """桩：模拟 NSAppearance，只提供 name()。"""
    def __init__(self, name):
        self._name = name

    def name(self):
        return self._name


class _FakeNSApp(object):
    """桩：模拟 NSApp，外观可在两次调用之间切换（复现系统外观切换）。"""
    def __init__(self, name):
        self.name_ = name

    def effectiveAppearance(self):
        return _FakeAppearance(self.name_)


class _GlassHost(sf_glass.GlassMixin):
    """只挂 GlassMixin 的轻量宿主：_glass_dim 不依赖窗口几何/Qt，无需 QApplication。"""


class TestAppearancePureFuncs(unittest.TestCase):
    """纯函数：不读全局状态，输入外观 → 输出暗化度。"""

    def test_mode_from_name(self):
        f = sf_glass.appearance_mode_from_name
        self.assertEqual(f("NSAppearanceNameDarkAqua"), "dark")
        self.assertEqual(f("NSAppearanceNameVibrantDark"), "dark")
        self.assertEqual(f("NSAppearanceNameAccessibilityHighContrastDarkAqua"), "dark")
        self.assertEqual(f("NSAppearanceNameAqua"), "light")
        self.assertEqual(f("NSAppearanceNameVibrantLight"), "light")
        # 空/None 按 light 处理（与修复前判断逐字等价）
        self.assertEqual(f(""), "light")
        self.assertEqual(f(None), "light")
        self.assertEqual(f("SomeFutureAppearance"), "light")

    def test_dim_for_mode(self):
        self.assertEqual(sf_glass.glass_dim_for_mode("light"), BG_GLASS_DIM_LIGHT)
        self.assertEqual(sf_glass.glass_dim_for_mode("dark"), BG_GLASS_DIM)
        # 浅色档必须比暗色档更深，否则浅色壁纸上白字看不清
        self.assertGreater(sf_glass.glass_dim_for_mode("light"),
                           sf_glass.glass_dim_for_mode("dark"))
        # 未知模式按 dark 处理，不抛异常
        self.assertEqual(sf_glass.glass_dim_for_mode("unknown"), BG_GLASS_DIM)

    def test_surface_rim_is_subtle_and_adapts_to_appearance(self):
        """玻璃受光边必须比正文弱，并在深浅外观间保持不同层次。"""
        dark = sf_glass.glass_surface_rim_for_mode("dark")
        light = sf_glass.glass_surface_rim_for_mode("light")
        self.assertEqual(len(dark), 4)
        self.assertEqual(len(light), 4)
        self.assertNotEqual(dark, light)
        for rim in (dark, light):
            self.assertLessEqual(rim[3], 16, "受光边不得抢过行情正文")
            self.assertGreaterEqual(rim[3], 1)


class TestGlassDimFollowsAppearance(unittest.TestCase):
    """回归核心：外观切换后，不做任何失效复位，暗化度也必须跟着变。"""

    def test_dim_updates_without_invalidation(self):
        host = _GlassHost()
        app = _FakeNSApp("NSAppearanceNameDarkAqua")
        with patch("AppKit.NSApp", app, create=True):
            self.assertEqual(host._glass_dim(), BG_GLASS_DIM)
            # 模拟用户在系统设置里切到浅色：不发任何屏幕事件、不复位缓存
            app.name_ = "NSAppearanceNameAqua"
            self.assertEqual(host._glass_dim(), BG_GLASS_DIM_LIGHT,
                             "外观切到浅色后暗化度未更新（陈旧缓存回归）")
            # 再切回深色同样要立即生效
            app.name_ = "NSAppearanceNameDarkAqua"
            self.assertEqual(host._glass_dim(), BG_GLASS_DIM)

    def test_stale_glass_mode_attr_ignored(self):
        """旧缓存在实例上留有陈旧值时也不得被采纳（防止重新引入缓存读取）。"""
        host = _GlassHost()
        host._glass_mode = "dark"          # 陈旧缓存残留
        with patch("AppKit.NSApp", _FakeNSApp("NSAppearanceNameAqua"), create=True):
            self.assertEqual(host._glass_dim(), BG_GLASS_DIM_LIGHT)

    def test_detection_failure_falls_back_to_dark(self):
        """AppKit 不可用 / NSApp 为 None / 探测抛异常：一律兜底 dark 档，不抛异常。"""
        host = _GlassHost()
        with patch("AppKit.NSApp", None, create=True):
            self.assertEqual(host._glass_dim(), BG_GLASS_DIM)
        with patch("AppKit.NSApp", _FakeNSApp("NSAppearanceNameAqua"), create=True), \
             patch.object(_FakeNSApp, "effectiveAppearance",
                          side_effect=RuntimeError("boom")):
            self.assertEqual(host._glass_dim(), BG_GLASS_DIM)

    def test_detect_system_appearance_maps_real_nsapp(self):
        with patch("AppKit.NSApp", _FakeNSApp("NSAppearanceNameVibrantDark"), create=True):
            self.assertEqual(sf_glass.detect_system_appearance(), "dark")
        with patch("AppKit.NSApp", _FakeNSApp("NSAppearanceNameAqua"), create=True):
            self.assertEqual(sf_glass.detect_system_appearance(), "light")


if __name__ == "__main__":
    unittest.main()
