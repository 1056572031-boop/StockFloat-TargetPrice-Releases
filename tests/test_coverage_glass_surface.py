# -*- coding: utf-8 -*-
"""sf_glass 覆盖率专项第二批（遗留 C2 续）：玻璃层的「表面」与「生命周期」。

第一批（`tests/test_coverage_glass_layer.py`）补的是**判定与几何核对**；这一批补剩下的
那一整块「把背景层画出来、摆上去、收掉」的代码：

  · 弹窗表面：`popup_stylesheet` / `paint_popup_surface` / `PopupLineEdit.paintEvent` /
    `prepare_popup_surface` —— 失效表现是「编辑窗是方的、四周多一道白边、输入框看不见」；
  · 外观跟随：`appearance_mode_from_name` / `glass_dim_for_mode` /
    `glass_surface_rim_for_mode` / `detect_system_appearance` —— 失效表现是
    「切成浅色系统外观后白字对比度不足」（v1.2.65 的真事故：探测结果被跨绘制缓存）；
  · 圆角几何：`_glass_squircle_cgpath` 与 Qt 侧 `smooth_round_rect_path` 的一致性 ——
    失效表现是「四角出现大圆角套小圆角的双层圆角」；
  · 玻璃层生命周期：`_init_glass_state` / `_inject_glass` / `_close_glass` /
    `_set_glass_mask` / `_sync_glass_frame` / `_glass_follow_tick` /
    `_glass_follow_begin` —— 失效表现是「背景层停在别处」「松手之后还在 8ms 一跑」。

刻意**不**在本批做的：`_setup_glass`。它要在屏幕上建 NSWindow，而同进程建 AppKit 窗口
历史上会冲掉整个测试会话（见 `tests/_alert_child.py` 的取舍），所以它由子进程 harness
负责：`tests/_glass_child.py` + `tests/test_coverage_glass_setup_chain.py`。

判据一律走可观测输出：像素、CGPath 的「点在形内还是形外」、真正挂到 view 上的 mask
几何、推给 AppKit 的 frame、run loop 唤醒后背景窗的实际位置。不读源码做字符串断言
（AGENTS §6 第 2 条）。
"""

import os
import sys
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import AppKit                      # 真模块：外观探测与常量都要用它
import CoreFoundation
import Quartz                      # 只打函数桩，绝不动 sys.modules（PyObjC 会 Reload 报错）
from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QImage, QPainter
from PyQt6.QtWidgets import QLineEdit

import sf_constants
import sf_glass
from renderer import _glass_squircle_cgpath, smooth_round_rect_path
from sf_glass import GlassMixin, PopupLineEdit
from tests._base import SFCase

_QAPP = None


def _ensure_qapp():
    """QApplication 必须由模块级引用持有，否则离屏下会中途 abort（同 test_v1443 的做法）。"""
    global _QAPP
    if _QAPP is None:
        _QAPP = SFCase.ensure_qapp()
    return _QAPP


_set_glass_mask = GlassMixin._set_glass_mask        # discipline-ok(R5): 被测对象本身就是私有实现
_sync_frame = GlassMixin._sync_glass_frame          # discipline-ok(R5): 同上
_follow_tick = GlassMixin._glass_follow_tick        # discipline-ok(R5): 同上
_follow_begin = GlassMixin._glass_follow_begin      # discipline-ok(R5): 同上
_follow_end = GlassMixin._glass_follow_end          # discipline-ok(R5): 同上
_init_state = GlassMixin._init_glass_state          # discipline-ok(R5): 同上
_inject = GlassMixin._inject_glass                  # discipline-ok(R5): 同上
_close = GlassMixin._close_glass                    # discipline-ok(R5): 同上
_mask_flush = GlassMixin._glass_mask_flush          # discipline-ok(R5): 同上

_GRACE = sf_glass._GLASS_FOLLOW_GRACE    # discipline-ok(R5): 宽限期是被测行为的一部分，只能从实现取值
_INSET = sf_glass.GLASS_INSET            # 玻璃层内缩量：mask 几何的直接判据，取唯一来源
_CORNER_R = sf_constants.R               # 主窗/弹窗共用圆角，取唯一来源而不是抄 30

# 弹窗表面的取样尺寸：必须 > 2R，否则 squircle 的四角会互相吃掉，
# 「四角透明」这条判据就退化成「整块透明」。
SURFACE_W = 2 * _CORNER_R + 40
SURFACE_H = 2 * _CORNER_R + 20

# 主窗取样尺寸：184x224 是产品默认卡片尺寸，300 是「拉伸之后」的那一个新高度。
MAIN_W, MAIN_H, GROWN_H = 184, 224, 300
# 内容窗在模型里的原点（假 NSWindow 的 frame 也从这里来）
MODEL_X, MODEL_Y = 100, 200


def _pixel(img, x, y):
    c = img.pixelColor(x, y)
    return (c.red(), c.green(), c.blue(), c.alpha())


class _Box:
    """只带 width()/height() 的绘制面（`paint_popup_surface` 只要这两个）。"""

    def __init__(self, w=SURFACE_W, h=SURFACE_H):
        self.size = (w, h)

    def width(self):
        return self.size[0]

    def height(self):
        return self.size[1]


class _Recorder:
    """替 `paint_popup_surface` 收一笔笔绘制调用：画了哪条路径、用了哪支笔。

    为什么用记录器而不全是像素：像素能证明「圆角真的裁出来了」，但证不了「受光边
    内缩了 1pt、没有画到外缘」—— 那正是历史上「双层轮廓」故障的成因，需要直接
    比两条路径的几何。两者各测一段，不互相顶替。
    """

    def __init__(self):
        self.filled = []
        self.drawn = []
        self.pens = []
        self.brushes = []

    def fillPath(self, path, brush):
        self.filled.append((path, brush))

    def drawPath(self, path):
        self.drawn.append(path)

    def setPen(self, pen):
        self.pens.append(pen)

    def setBrush(self, brush):
        self.brushes.append(brush)


class _Win:
    """假 NSWindow：只实现本轮方法真正会碰的那几个动作，并记录它们。"""

    def __init__(self, number=7, frame=None, raise_on=()):
        self.num = number
        self.rect = frame if frame is not None else Quartz.CGRectMake(MODEL_X, MODEL_Y, MAIN_W, MAIN_H)
        self.faults = set(raise_on)
        self.moved = []
        self.shadow = []
        self.opaque = []
        self.hidden = []

    def _maybe_raise(self, name):
        if name in self.faults:
            raise RuntimeError(name + " refused")

    def frame(self):
        return self.rect

    def windowNumber(self):
        return self.num

    def setHasShadow_(self, flag):
        self.shadow.append(flag)

    def setOpaque_(self, flag):
        self.opaque.append(flag)

    def setFrame_display_(self, rect, display):
        self._maybe_raise("setFrame_display_")
        self.moved.append((rect.origin.x, rect.origin.y,
                           rect.size.width, rect.size.height, display))

    def orderOut_(self, _sender):
        self._maybe_raise("orderOut_")
        self.hidden.append("out")


class _Layer:
    """假 CALayer（NSVisualEffectView 的 layer）：记下 setMask_ 挂上来的东西。"""

    def __init__(self):
        self.masks = []

    def setMask_(self, layer):
        self.masks.append(layer)


class _View:
    """假 NSVisualEffectView。`no_layer=True` 模拟 view 拿不到 backing layer。"""

    def __init__(self, no_layer=False):
        self.layer_obj = None if no_layer else _Layer()
        self.wants_layer = []

    def setWantsLayer_(self, flag):
        self.wants_layer.append(flag)

    def layer(self):
        return self.layer_obj


class _GlassHost:
    """`GlassMixin` 那批生命周期方法用到的宿主，属性一次配齐。

    为什么全部显式给值：这些属性有一整份是由 `_init_glass_state` / `_setup_glass`
    写下的，缺一个就会在方法里读到 AttributeError 并被兜底的 try/except 吞掉 ——
    那正是本仓 §6.9 说的「功能不见了但毫无痕迹」。
    """

    _set_glass_mask = _set_glass_mask
    _sync_glass_frame = _sync_frame
    _glass_follow_tick = _follow_tick
    _glass_follow_begin = _follow_begin
    _glass_follow_end = _follow_end
    _init_glass_state = _init_state
    _inject_glass = _inject
    _close_glass = _close
    _content_real_bounds = GlassMixin._content_real_bounds     # discipline-ok(R5): 被测对象本身就是私有实现
    _glass_mask_flush = _mask_flush                           # discipline-ok(R5): 同上

    def __init__(self, w=MAIN_W, h=MAIN_H, **extra):
        self.size = (w, h)
        self.updated = []
        self.moves = []
        self.mask_calls = []
        self._bg_effect = True
        self._glass_view = None
        self._glass_window = None
        self._glass_mask_layer = None
        self._glass_mask_pending = False
        self._glass_mask_w = 0
        self._glass_mask_h = 0
        self._last_glass_mask_ts = 0.0
        self._cgs_ok = None
        self._glass_pending = True
        self._glass_follow_timer = None
        self._glass_follow_until = 0.0
        self._glass_follow_pos = None
        self._glass_follow_cnum = None
        self._glass_follow_sh = 0.0
        for key, value in extra.items():
            setattr(self, key, value)

    def width(self):
        return self.size[0]

    def height(self):
        return self.size[1]

    def x(self):
        return MODEL_X

    def y(self):
        return MODEL_Y

    def update(self):
        self.updated.append("update")

    def move(self, x, y):
        self.moves.append((x, y))


def _answers(rect):
    """把宿主向 WindowServer 的那一问换成固定回答（None 表示「查不到」）。"""
    return lambda cnum=None, sh=None: rect


class PopupSurfaceTest(SFCase):
    """两个编辑弹窗共用的表面：圆角、共用样式、内侧受光边。"""

    def test_stylesheet_is_one_source_and_still_extensible(self):
        base = sf_glass.popup_stylesheet()
        extra = sf_glass.popup_stylesheet("QLabel{color:#123456;}")
        self.assertTrue(extra.endswith("QLabel{color:#123456;}"),
                        "追加段必须接在共用样式之后，两个弹窗才不会出现样式漂移")
        self.assertLess(len(base), len(extra))
        # 输入框自绘圆角，样式表必须把 Qt 自己的底和边撤掉，否则出现重复描边
        self.assertIn("QLineEdit{background:transparent", base)
        self.assertIn("border:none", base)

    def test_surface_uses_shared_translucent_material_and_clear_corners(self):
        _ensure_qapp()
        img = QImage(SURFACE_W, SURFACE_H, QImage.Format.Format_ARGB32)
        img.fill(Qt.GlobalColor.transparent)
        painter = QPainter(img)
        sf_glass.paint_popup_surface(_Box(), painter)
        painter.end()

        center = _pixel(img, SURFACE_W // 2, SURFACE_H // 2)
        self.assertTrue(all(abs(got - want) <= 1
                            for got, want in zip(center[:3], sf_glass.POPUP_SURFACE_RGB)),
                        "表面色只有一个来源，改了要能在像素上看见")
        self.assertEqual(center[3], int(sf_glass.POPUP_BG_ALPHA * 255),
                         "弹窗表面必须沿用主窗的半透明材质强度")
        for cx, cy in ((0, 0), (SURFACE_W - 1, 0), (0, SURFACE_H - 1),
                       (SURFACE_W - 1, SURFACE_H - 1)):
            self.assertEqual(_pixel(img, cx, cy)[3], 0,
                             "四角必须全透明：方角会在窗口外露出一层背景")

    def test_rim_hugs_the_inner_edge_and_never_the_outer_border(self):
        """受光边只画在表面内侧 1pt，不画到外缘 —— 画到外缘就是「双层轮廓」那次反馈。"""
        rec = _Recorder()
        sf_glass.paint_popup_surface(_Box(), rec)
        self.assertEqual(len(rec.filled), 1, "表面只铺一次")
        self.assertEqual(len(rec.drawn), 1, "受光边只描一圈")
        outer = rec.filled[0][0].boundingRect()
        inner = rec.drawn[0].boundingRect()
        self.assertAlmostEqual(inner.left() - outer.left(), 1.0, delta=0.5)
        self.assertLess(inner.width(), outer.width())
        self.assertEqual(rec.pens[-1].color().alpha(),
                         sf_glass.POPUP_SURFACE_HIGHLIGHT_ALPHA,
                         "受光边强度只有一个来源，不能悄悄画成实线")
        self.assertEqual(rec.brushes[-1], Qt.BrushStyle.NoBrush,
                         "描边那一步不能带填充刷，否则会把刚铺好的表面盖掉")

    def test_prepare_only_drops_the_shadow_and_schedules_a_repaint(self):
        """本应用没有录屏权限，弹窗表面必须只关阴影、不读屏幕。"""
        _ensure_qapp()
        edit = self.track_widget(QLineEdit())
        with mock.patch.object(sf_glass, "disable_native_window_shadow") as off, \
             mock.patch.object(sf_glass, "content_ns_window") as probe:
            sf_glass.prepare_popup_surface(edit)
        off.assert_called_once_with(edit)
        self.assertEqual(probe.call_count, 0,
                         "关阴影这条路径不该再去伸手拿内容窗")

    def test_input_field_paints_a_rounded_transparent_body(self):
        _ensure_qapp()
        edit = self.track_widget(PopupLineEdit())
        edit.setStyleSheet(sf_glass.popup_stylesheet())
        edit.resize(sf_glass.POPUP_TARGET_INPUT_W, sf_glass.POPUP_INPUT_H)
        img = edit.grab().toImage()
        self.assertEqual(_pixel(img, 0, 0)[3], 0, "输入框四角必须透明（自绘圆角生效）")
        self.assertGreater(_pixel(img, img.width() // 2, img.height() // 2)[3], 0,
                           "输入框主体必须看得见，否则整条自绘等于没画")

    def test_disabled_field_is_dimmer_than_the_usable_one(self):
        _ensure_qapp()
        edit = self.track_widget(PopupLineEdit())
        edit.setStyleSheet(sf_glass.popup_stylesheet())
        edit.resize(sf_glass.POPUP_TARGET_INPUT_W, sf_glass.POPUP_INPUT_H)

        def center_alpha():
            img = edit.grab().toImage()
            return _pixel(img, img.width() // 2, img.height() // 2)[3]

        on = center_alpha()
        edit.setEnabled(False)
        off = center_alpha()
        self.assertLess(off, on,
                        "禁用态底更暗是唯一的「不能编辑」提示；两条分支画得一样，"
                        "用户就分不清能不能输入")

    def test_focus_border_is_more_visible_than_the_blurred_one(self):
        _ensure_qapp()
        edit = self.track_widget(PopupLineEdit())
        edit.setStyleSheet(sf_glass.popup_stylesheet())
        edit.resize(sf_glass.POPUP_TARGET_INPUT_W, sf_glass.POPUP_INPUT_H)
        mid_x = edit.width() // 2

        def edge_alpha():
            # 描边实测只占最外一圈（y=0），y=1 已经是主体底色
            return _pixel(edit.grab().toImage(), mid_x, 0)[3]

        blurred = edge_alpha()
        with mock.patch.object(edit, "hasFocus", return_value=True):
            focused = edge_alpha()
        self.assertGreater(focused, blurred,
                           "聚焦描边必须比未聚焦更显眼，否则看不出光标在哪个框里")


class AppearanceFollowTest(SFCase):
    """系统深/浅色外观 → 玻璃暗化度：v1.2.65 那次「缓存永久陈旧」的事故守在这里。"""

    def test_dark_only_when_the_name_actually_says_dark(self):
        cases = [("NSAppearanceNameDarkAqua", "dark"),
                 ("NSAppearanceNameVibrantDark", "dark"),
                 ("NSAppearanceNameAqua", "light"),
                 ("", "light"), (None, "light")]
        for name, want in cases:
            with self.subTest(name=name):
                self.assertEqual(sf_glass.appearance_mode_from_name(name), want)

    def test_light_appearance_uses_the_deeper_dim(self):
        """浅色壁纸上白字要更暗的底 —— 两个数字都有唯一来源（sf_constants）。"""
        self.assertEqual(sf_glass.glass_dim_for_mode("light"),
                         sf_constants.BG_GLASS_DIM_LIGHT)
        self.assertEqual(sf_glass.glass_dim_for_mode("dark"), sf_constants.BG_GLASS_DIM)
        self.assertGreater(sf_glass.glass_dim_for_mode("light"),
                           sf_glass.glass_dim_for_mode("dark"))
        self.assertLessEqual(sf_glass.glass_dim_for_mode("light"), 255,
                             "暗化 alpha 必须在 0~255 内，超出就是画不出来的一坨")

    def test_rim_gets_softer_in_the_light_appearance(self):
        light = sf_glass.glass_surface_rim_for_mode("light")
        dark = sf_glass.glass_surface_rim_for_mode("dark")
        self.assertEqual(len(light), 4)
        self.assertLess(light[3], dark[3],
                        "浅色外观下受光边更弱，否则窗口边缘会出现一条亮框")

    def test_without_an_app_instance_it_falls_back_to_dark(self):
        """离屏进程里没有 NSApp 实例 —— 兜底必须是 dark（原设计），不是抛出去。"""
        self.assertEqual(sf_glass.detect_system_appearance(), "dark")

    def test_the_appearance_is_read_every_single_time(self):
        """关键一条：探测结果**不许**跨调用缓存。

        v1.2.65 之前 `_glass_dim` 把结果存进 `self._glass_mode`，而 macOS 切深/浅色
        不触发任何屏幕事件 → 缓存永远陈旧，浅色外观下白字对比度不足。
        这里连问两次、中途换掉系统外观，第二次必须跟着变。
        """
        def app(name):
            return types.SimpleNamespace(
                effectiveAppearance=lambda: types.SimpleNamespace(name=lambda: name))

        with mock.patch.object(AppKit, "NSApp", app("NSAppearanceNameAqua")):
            self.assertEqual(sf_glass.detect_system_appearance(), "light")
            with mock.patch.object(AppKit, "NSApp", app("NSAppearanceNameDarkAqua")):
                self.assertEqual(sf_glass.detect_system_appearance(), "dark",
                                 "外观换了而探测没跟上，就是当年那个 bug 回来了")

    def test_a_broken_appearance_read_never_reaches_the_user(self):
        def boom():
            raise RuntimeError("no appearance")

        with mock.patch.object(AppKit, "NSApp",
                               types.SimpleNamespace(effectiveAppearance=boom)):
            self.assertEqual(sf_glass.detect_system_appearance(), "dark")


class AppKitHandshakeGuardTest(SFCase):
    """凡是「从 Qt 伸手进 AppKit」的入口都必须先问平台 —— 答错不是异常，是段错误。"""

    def test_offscreen_platform_returns_no_window(self):
        _ensure_qapp()
        edit = self.track_widget(QLineEdit())
        self.assertFalse(sf_glass.native_cocoa_active())
        self.assertIsNone(sf_glass.content_ns_window(edit),
                          "离屏下 winId() 不是 NSView*，取不到才是对的")

    def test_a_failing_handshake_leaves_a_trace(self):
        """这里静默返回 None = 置顶与背景层同步从此再没人做，必须留痕（AGENTS §6 第 9 条）。"""
        with mock.patch.object(sf_glass, "native_cocoa_active", return_value=True), \
             mock.patch.object(sf_glass, "_objc_deps",       # discipline-ok(R5): 要测的就是这个私有入口失败时的留痕
                               side_effect=RuntimeError("no pyobjc")), \
             mock.patch.object(sf_glass, "log_swallow") as swallow:
            self.assertIsNone(sf_glass.content_ns_window(object()))
        swallow.assert_called_once()
        self.assertIn("content_ns_window", swallow.call_args[0][0])


class SquircleParityTest(SFCase):
    """Qt 侧圆角与 CoreGraphics 侧圆角必须同一个形状 —— 用「点在形内还是形外」判。

    只比包围盒不够：直角矩形的包围盒和 squircle 一模一样，抓不到「弧线不一样」。
    而四角双层圆角重叠（真实用户反馈）正是弧线不一致造成的。
    """

    W, H = MAIN_W, MAIN_H

    def _paths(self):
        cg = _glass_squircle_cgpath(0, 0, self.W, self.H, _CORNER_R)
        qt = smooth_round_rect_path(0, 0, self.W, self.H, _CORNER_R)
        return cg, qt

    def test_both_pipelines_agree_on_every_sampled_point(self):
        """比 shape 而不是比写法：同一个点，两边必须给同一个"在/不在"。

        取样一律用 .5 偏移：整数坐标正好压在矩形边上，而 CG 与 Qt 对**边界点**
        的归属约定不同（实测 y=H 那一行 CG 说在内、Qt 说在外）。那是端点约定，
        不是形状差别，把它当形状差别会得到一条永远解释不清的红。
        """
        cg, qt = self._paths()
        step = 4
        points = [(px + 0.5, py + 0.5) for px in range(0, self.W, step)
                  for py in range(0, self.H, step)]
        # 四角弧线附近单点加密：网格步长 4pt 会把弧线的细微差别整片放过
        for cx, cy in ((0, 0), (self.W, 0), (0, self.H), (self.W, self.H)):
            for dx in range(1, _CORNER_R, 3):
                points.append((cx + (dx if cx == 0 else -dx) - 0.5,
                               cy + (dx if cy == 0 else -dx) - 0.5))
        mismatches = []
        for px, py in points:
            inside_cg = bool(Quartz.CGPathContainsPoint(cg, None,
                                                        Quartz.CGPointMake(px, py), False))
            inside_qt = bool(qt.contains(QPointF(px, py)))
            if inside_cg != inside_qt:
                mismatches.append((px, py, inside_cg, inside_qt))
        self.assertGreater(len(points), 1000, "取样太少等于没比")
        self.assertEqual(mismatches[:5], [],
                         "两条流水线对同一个点的判定不一致 = 四角会出现大圆角套小圆角")

    def test_the_corners_are_actually_cut(self):
        """形状判据要有正反两面：如果两条路径都是整块矩形，上一条测试照样绿。"""
        cg, qt = self._paths()
        for px, py in ((1, 1), (self.W - 2, 1), (1, self.H - 2), (self.W - 2, self.H - 2)):
            self.assertFalse(Quartz.CGPathContainsPoint(cg, None,
                                                        Quartz.CGPointMake(px, py), False),
                             "CG 侧四角必须被裁掉：(%d,%d) 还在形内" % (px, py))
            self.assertFalse(qt.contains(QPointF(px, py)),
                             "Qt 侧四角必须被裁掉：(%d,%d) 还在形内" % (px, py))
        self.assertTrue(Quartz.CGPathContainsPoint(cg, None,
                                                  Quartz.CGPointMake(self.W // 2, self.H // 2),
                                                  False),
                        "中心还在形内，否则上面那四条只是「整块都不在」")

    def test_a_tiny_window_still_gets_a_mask_without_error(self):
        """比圆角还窄的尺寸是拉伸到极限时真会出现的：不许抛，也不许把失败咽掉。"""
        host = _GlassHost()
        view = _View()
        with mock.patch.object(sf_glass, "log_line") as line:
            _set_glass_mask(host, view, 5, 5)
        self.assertIsNotNone(getattr(host, "_glass_mask_layer", None),
                             "小尺寸也要建出 mask layer，否则玻璃层退回直角")
        self.assertEqual(line.call_args_list, [],
                         "没有失败却写了 glass mask error，等于把日志变成噪声")


class GlassLifecycleTest(SFCase):
    """子窗/主窗共用的那套玻璃状态：初始化、降级、收摊，一个都不能少写。"""

    def test_init_primes_every_field_a_consumer_reads(self):
        host = _GlassHost()
        _init_state(host)
        # 这些属性一旦被漏写，消费方读不到就走 try/except 静默 —— 置顶从此没人做
        self.assertIs(getattr(host, "_glass_window", "缺"), None)
        self.assertIs(getattr(host, "_glass_view", "缺"), None)
        self.assertIs(getattr(host, "_glass_mask_layer", "缺"), None)
        self.assertFalse(getattr(host, "_glass_mask_pending", True))
        self.assertEqual((getattr(host, "_glass_mask_w", -1),
                          getattr(host, "_glass_mask_h", -1)), (0, 0))
        self.assertIs(getattr(host, "_glass_follow_timer", "缺"), None)
        self.assertEqual(getattr(host, "_glass_follow_sh", -1.0), 0.0)
        self.assertTrue(getattr(host, "_glass_pending", False),
                        "子窗默认要尝试毛玻璃，非 macOS 才自动降级")
        # 初始化之后三个消费方法都必须能直接跑，且不凭空造出任何动作
        _sync_frame(host)
        _mask_flush(host)
        _close(host)
        self.assertEqual(host.mask_calls, [])
        self.assertEqual(host.moves, [])

    def test_inject_downgrades_the_child_window_to_qt_translucency(self):
        """v1.2.90：子窗不再建 NSVisualEffectView（现场连续崩在 objc_release）。"""
        host = _GlassHost(_glass_window=_Win(), _glass_view=_View())
        _inject(host)
        self.assertFalse(host._bg_effect)                    # discipline-ok(R5): 这条降级标记没有公开出口，只能直接读
        self.assertIsNone(getattr(host, "_glass_window", "还在"))
        self.assertIsNone(getattr(host, "_glass_view", "还在"))
        self.assertFalse(getattr(host, "_glass_pending", True),
                         "子窗不许再排一次毛玻璃注入")
        self.assertEqual(host.updated, ["update"])

    def test_close_hides_the_layer_and_resets_the_reuse_flags(self):
        """面板被复用（「有未生效修改」那条分支）时，收摊必须把状态清干净。"""
        gw = _Win()
        host = _GlassHost(_glass_window=gw, _glass_mask_w=MAIN_W, _glass_mask_h=GROWN_H,
                          _glass_mask_pending=True, _glass_pending=False,
                          _glass_mask_layer=object())
        _close(host)
        self.assertEqual(gw.hidden, ["out"], "只能 orderOut，不能 close（双重释放会崩）")
        self.assertTrue(getattr(host, "_glass_pending", False),
                        "不置回 True，实例再 show 时永远只有降级深蓝卡")
        self.assertEqual((getattr(host, "_glass_mask_w", -1),
                          getattr(host, "_glass_mask_h", -1)), (0, 0),
                         "尺寸缓存留着，下一次 mask 更新会被判成 skip，圆角不按新尺寸重建")
        self.assertFalse(getattr(host, "_glass_mask_pending", True))
        self.assertIsNone(getattr(host, "_glass_mask_layer", "还在"))

    def test_close_still_resets_when_the_layer_refuses_to_hide(self):
        gw = _Win(raise_on=("orderOut_",))
        host = _GlassHost(_glass_window=gw)
        _close(host)
        self.assertEqual(gw.hidden, [], "隐藏的失败不许变成崩溃")
        self.assertTrue(getattr(host, "_glass_pending", False),
                        "收不掉背景窗不能连带把复用状态也留脏")


class GlassMaskBuildTest(SFCase):
    """圆角 mask 的建与重建：只挂一次、几何跟主窗同源、失败必须留痕。"""

    def test_first_build_attaches_one_layer_with_the_inset_geometry(self):
        host = _GlassHost()
        view = _View()
        _set_glass_mask(host, view, MAIN_W, MAIN_H)
        layer = getattr(host, "_glass_mask_layer", None)
        self.assertIsNotNone(layer, "第一次必须建出 mask layer，否则玻璃层是直角")
        self.assertEqual(view.layer().masks, [layer], "mask 必须挂到 view 自己的 layer 上")
        self.assertEqual(view.wants_layer, [True])
        box = Quartz.CGPathGetBoundingBox(layer.path())
        self.assertAlmostEqual(box.origin.x, _INSET, places=2)
        self.assertAlmostEqual(box.size.width, MAIN_W - 2 * _INSET, places=2)
        self.assertAlmostEqual(box.size.height, MAIN_H - 2 * _INSET, places=2,
                               msg="玻璃层边缘材质自带 1~3px 描边，整体内缩后由主窗透明边缘接管")

    def test_rebuild_reuses_the_layer_and_only_moves_geometry(self):
        host = _GlassHost()
        view = _View()
        _set_glass_mask(host, view, MAIN_W, MAIN_H)
        first = host._glass_mask_layer                       # discipline-ok(R5): 判「同一个 layer 被复用」必须拿到它
        _set_glass_mask(host, view, MAIN_W, GROWN_H)
        self.assertIs(host._glass_mask_layer, first,          # discipline-ok(R5): 同上
                      "每次重建都新造 layer 会把旧的悬在 view 上，圆角会闪")
        self.assertEqual(len(view.layer().masks), 1, "第二次不该再挂一次 mask")
        box = Quartz.CGPathGetBoundingBox(first.path())
        self.assertAlmostEqual(box.size.height, GROWN_H - 2 * _INSET, places=2)
        self.assertEqual(first.frame().size.height, GROWN_H,
                         "mask 的 frame 必须跟着 bounds 走，否则 layer 变了 mask 错位")

    def test_rasterize_failure_is_written_to_the_log(self):
        """mask 建不出来只表现为「四角是方的」，功能不坏、用户难自查 —— 必须留一行。"""
        host = _GlassHost()
        view = _View(no_layer=True)
        with mock.patch.object(sf_glass, "log_line") as line:
            _set_glass_mask(host, view, MAIN_W, MAIN_H)
        messages = [c[0][0] for c in line.call_args_list]
        self.assertTrue(any("glass mask error" in m for m in messages),
                        "view 拿不到 backing layer 时不能静默返回，四角会悄悄变直角")
        self.assertIsNone(getattr(host, "_glass_mask_layer", "写了"),
                          "失败后不许留下半挂的 layer")

    def test_a_view_that_throws_is_traced_too(self):
        host = _GlassHost()
        view = _View()
        view.setWantsLayer_ = lambda flag: (_ for _ in ()).throw(RuntimeError("no layer"))
        with mock.patch.object(sf_glass, "log_line") as line:
            _set_glass_mask(host, view, MAIN_W, MAIN_H)
        self.assertTrue(any("glass mask error" in c[0][0] for c in line.call_args_list))


class SyncGlassFrameTest(SFCase):
    """主窗移动/拉伸时把玻璃层摆过去：位置、提交方式、圆角重建的三态计划。"""

    def _host(self, gw, **extra):
        calls = []
        host = _GlassHost(_glass_window=gw, _glass_view=_View(),
                          _set_glass_mask=lambda v, w, h: calls.append((w, h)))
        for key, value in extra.items():
            setattr(host, key, value)
        return host, calls

    def test_no_glass_layer_writes_nothing(self):
        host = _GlassHost(_glass_window=None)
        with mock.patch.object(sf_glass, "content_ns_window") as probe:
            _sync_frame(host)
        self.assertEqual(probe.call_count, 0,
                         "没有背景层时连内容窗都不该去问，省电也是这条路径的一部分")

    def test_no_content_window_leaves_the_background_where_it_is(self):
        gw = _Win()
        host, _calls = self._host(gw)
        with mock.patch.object(sf_glass, "content_ns_window", return_value=None):
            _sync_frame(host)
        self.assertEqual(gw.moved, [], "宁可不挪，也不能把背景层摆到猜的位置")

    def test_frame_follows_the_model_without_forcing_a_repaint(self):
        gw = _Win()
        host, _calls = self._host(gw)
        with mock.patch.object(sf_glass, "content_ns_window", return_value=gw):
            _sync_frame(host)
        self.assertEqual(len(gw.moved), 1)
        x, y, w, h, display = gw.moved[0]
        self.assertEqual((x, y, w, h), (MODEL_X, MODEL_Y, float(MAIN_W), float(MAIN_H)))
        self.assertFalse(display,
                         "display=False 交给系统合成周期；每次 resize 都强制立即重绘"
                         "就是拉伸闪跳的成因")

    def test_server_origin_is_used_only_when_asked(self):
        """`use_server_origin=True` 用在「已经判出模型不可信」的两处：位置问服务端，尺寸仍走模型。"""
        gw = _Win()
        host, _calls = self._host(gw)
        host._content_real_bounds = _answers((900.0, 400.0, 999.0, 999.0))  # discipline-ok(R5): 换掉那一问的回答
        with mock.patch.object(sf_glass, "content_ns_window", return_value=gw):
            _sync_frame(host, use_server_origin=True)
        x, y, w, h, _display = gw.moved[0]
        self.assertEqual((x, y), (900.0, 400.0), "背景层要摆到服务端真实位置")
        self.assertEqual((w, h), (float(MAIN_W), float(MAIN_H)),
                         "尺寸必须与画圆角用的 width()/height() 同源，"
                         "否则拉伸动画里会出现背景尺寸与圆角尺寸不一致的错位")

    def test_mask_plan_routes_the_rebuild_three_ways(self):
        """immediate 就地重建、pending 交给下一绘制帧、skip 什么都不做。"""
        def run(force, last_w):
            gw = _Win()
            calls = []
            host = _GlassHost(_glass_window=gw, _glass_view=_View(),
                              _glass_mask_w=last_w, _glass_mask_h=MAIN_H,
                              _glass_mask_pending=False,
                              _set_glass_mask=lambda v, w, h: calls.append((w, h)))
            with mock.patch.object(sf_glass, "content_ns_window", return_value=gw):
                _sync_frame(host, force_mask=force)
            return host, calls

        immediate, calls = run(True, 184)
        self.assertEqual(calls, [(MAIN_W, MAIN_H)],
                         "关键节点（拉伸结束/收展）必须本帧重建一次，保证圆角精确对齐")
        self.assertEqual((immediate._glass_mask_w,             # discipline-ok(R5): 尺寸缓存是内部记账，没有公开读数
                          immediate._glass_mask_h), (MAIN_W, MAIN_H))  # discipline-ok(R5): 同上
        self.assertFalse(getattr(immediate, "_glass_mask_pending", True))

        pending, calls = run(False, 100)
        self.assertEqual(calls, [], "尺寸变了但这一帧不重建：等主窗 paintEvent 同帧做")
        self.assertTrue(getattr(pending, "_glass_mask_pending", False))

        skip, calls = run(False, 184)
        self.assertEqual(calls, [])
        self.assertFalse(getattr(skip, "_glass_mask_pending", True),
                         "Δ≤1px 复用尺寸缓存，同时把 pending 清干净，不然每帧都重建")

    def test_appkit_refusal_is_traced(self):
        gw = _Win(raise_on=("setFrame_display_",))
        host, _calls = self._host(gw)
        with mock.patch.object(sf_glass, "content_ns_window", return_value=gw), \
             mock.patch.object(sf_glass, "log_swallow") as swallow:
            _sync_frame(host)
        self.assertEqual(swallow.call_count, 1,
                         "背景层摆不过去 = 用户看见「只剩背景」，这类失败不许静默")
        self.assertIn("_sync_glass_frame", swallow.call_args[0][0])


class FollowTimerTest(SFCase):
    """系统拖拽期间的背景层跟随：以 WindowServer 为准，活动续期，丢了手势要自停。"""

    def test_tick_without_a_layer_does_nothing(self):
        host = _GlassHost(_glass_window=None)
        _follow_tick(host)
        self.assertIsNone(getattr(host, "_glass_follow_pos", "写了"))

    def test_tick_without_an_answer_moves_nothing(self):
        gw = _Win()
        host = _GlassHost(_glass_window=gw)
        host._content_real_bounds = _answers(None)           # discipline-ok(R5): 换掉那一问的回答
        _follow_tick(host)
        self.assertEqual(gw.moved, [], "查不到真位置就不动，不能拿过时模型去摆")

    def test_moving_content_renews_the_grace_period(self):
        gw = _Win()
        host = _GlassHost(_glass_window=gw, _glass_follow_until=time.monotonic() - 1.0,
                          _glass_follow_pos=None)
        host._content_real_bounds = _answers((900.0, 400.0, float(MAIN_W), float(MAIN_H)))  # discipline-ok(R5): 同上
        _follow_tick(host)
        self.assertEqual(gw.moved[0][:4], (900.0, 400.0, float(MAIN_W), float(MAIN_H)))
        self.assertFalse(gw.moved[0][4],
                         "跟随每一拍都不许强制重绘（display=False），掉帧就是从这儿来的")
        self.assertGreater(host._glass_follow_until, time.monotonic(),   # discipline-ok(R5): 宽限期终点没有公开出口
                           "内容窗还在动 → 宽限期必须续上，否则慢速长拖过 8 秒就悄悄不跟了")

    def test_stationary_past_the_grace_stops_following(self):
        """丢了 mouseRelease 时最迟在最后一次移动之后自停，不留一个 8ms 定时器永久活着。"""
        pos = (900.0, 400.0, float(MAIN_W), float(MAIN_H))
        gw = _Win()
        host = _GlassHost(_glass_window=gw, _glass_follow_pos=pos,
                          _glass_follow_until=time.monotonic() - 0.1)
        host._content_real_bounds = _answers(pos)            # discipline-ok(R5): 同上
        _follow_tick(host)
        self.assertIsNone(getattr(host, "_glass_follow_pos", "还在"),
                          "宽限期到点必须收摊（判据来自 _glass_follow_end 的复位）")
        self.assertIsNone(getattr(host, "_glass_follow_timer", "还在"))

    def test_begin_without_a_layer_adds_no_timer(self):
        host = _GlassHost(_glass_window=None)
        _follow_begin(host)
        self.assertIsNone(getattr(host, "_glass_follow_timer", "建了"))

    def test_begin_without_a_content_window_installs_nothing(self):
        host = _GlassHost(_glass_window=_Win())
        with mock.patch.object(sf_glass, "content_ns_window", return_value=None):
            _follow_begin(host)
        self.assertIsNone(getattr(host, "_glass_follow_timer", "建了"))

    def test_begin_caches_geometry_then_the_timer_moves_the_layer(self):
        """端到端一条：定时器真被 run loop 唤醒、真的把背景层挪到了服务端位置。

        为什么值得付这最多 3 秒：`_glass_follow_begin` 整段被 try/except 兜着，
        回调里任何一处写错（比如 NSMakeRect 的参数顺序）都不会让测试进程红，
        只会让拖窗口时背景层不动 —— 那正是用户报的「背景停在别处」。
        真红的时候先看本机有没有窗口服务：`_glass_follow_sh` 为 0 就是取不到主屏。
        """
        gw = _Win()
        host = _GlassHost(_glass_window=gw)
        host._content_real_bounds = _answers((777.0, 333.0, float(MAIN_W), float(MAIN_H)))  # discipline-ok(R5): 同上
        with mock.patch.object(sf_glass, "content_ns_window",
                               return_value=_Win(number=7)):
            _follow_begin(host)
            cnum = getattr(host, "_glass_follow_cnum", None)
            sh = getattr(host, "_glass_follow_sh", 0.0)
            timer = getattr(host, "_glass_follow_timer", None)
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and not gw.moved:
                CoreFoundation.CFRunLoopRunInMode(
                    CoreFoundation.kCFRunLoopDefaultMode, 0.05, True)
            _follow_end(host)

        self.assertEqual(cnum, 7, "一段手势里窗口号恒定，先缓存下来免得每拍多付一次 winId()")
        self.assertGreater(sh, 0.0, "主屏高度取不到就没法做 CG→NS 换算")
        self.assertIsNotNone(timer, "begin 必须真的挂上一个 run loop 定时器")
        self.assertTrue(gw.moved, "3 秒内没被 run loop 唤醒：要么定时器没挂上，要么回调抛异常被吞了")
        self.assertEqual(gw.moved[0][:4], (777.0, 333.0, float(MAIN_W), float(MAIN_H)))
        self.assertIsNone(getattr(host, "_glass_follow_timer", "还在"),
                          "收摊必须把定时器交出去，不然就是永久 8ms 一跑")

    def test_the_grace_window_is_documented_in_the_implementation(self):
        """宽限期取的是实现里那个常量（唯一来源），这里只写来源与生效规则，不抄数字。"""
        self.assertGreater(_GRACE, 0.0, "宽限期为 0 等于「一丢手势就停跟」")


if __name__ == "__main__":
    unittest.main()
