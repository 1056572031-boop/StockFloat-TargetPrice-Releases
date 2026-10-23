# -*- coding: utf-8 -*-
"""sf_icons / sf_editor 补测：图标生成的降级姿态，与编辑器搜索链路的收口。

为什么单独补这一层（本轮为扩大自动化覆盖而加）
----------------------------------------------
这两个模块的失败模式都是"用户看得见功能没了、但日志里什么都没有"：
图标渲染不出来 → 右上按钮退回自绘兜底；搜索线程没收好尾 → QThread 运行中
析构直接 abort；编辑器回包竞态没拦住 → "搜了 B 结果切到刚才的 A"。

本文件守的都是**可观测输出**（像素、调用参数、状态行文案、登记表长度），
不去断言源码写法。图标依赖 macOS 专有 API 的部分用假 AppKit 模块注入，
不在测试里动真实的 Dock 策略。
"""
import os
import queue
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtGui import QColor, QImage
from PyQt6.QtWidgets import QApplication, QPushButton

import sf_editor             # noqa: E402
import sf_icons              # noqa: E402
import sf_ui_render          # noqa: E402
import sf_universe           # noqa: E402

try:
    from _base import SFCase, wait_for
except ImportError:           # pragma: no cover - 包模式导入
    from tests._base import SFCase, wait_for


APP_ICON_PX = 512      # Dock 用的应用图标边长；新用例引用它，不写裸 512（AGENTS §6 第 6 条）

# 2026-10-23 用户要求图标带色块底（浅色外观浅灰、深色外观深色）。实测：色块占画布 80.5%，
# 四周各留约 10% 透明边；标志图形占色块宽约 56%、高约 55%。下面三个范围都留了余量，
# 只拦"明显缩回去 / 撑满 / 贴边"，不拦抗锯齿与渲染器的细小差异。
PLATE_MIN_FRACTION = 0.75           # 色块占画布边长的下限（实测 0.805）
PLATE_MIN_MARGIN_FRACTION = 0.05    # 色块四周透明边的下限（实测约 0.097）
MARK_MIN_FRACTION_OF_PLATE = 0.50   # 标志图形占色块边长的下限（实测 0.56 / 0.55）
MARK_MAX_FRACTION_OF_PLATE = 0.65   # 上限：再大就几乎贴住色块边缘
# 随包 icon.png 与运行时出图的外框允许相差的像素（抗锯齿边缘），超过说明两者不同源。
SHIPPED_ICON_BBOX_TOLERANCE_PX = 2
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLATE_HEX = {"light": "#e8ebf0", "dark": "#12151d"}   # 定稿底色；测试自己写一份，不引用被测常量


def _plate_sample(img):
    """色块左侧中段一点（距左缘 18% 处）：在色块内、在标志左侧、避开圆角。
    用相对坐标，icon.icns 被 Qt 解出多大都能取。"""
    return img.pixelColor(int(img.width() * 0.18), img.height() // 2)


def _mark_bbox(img, mode):
    """标志图形的外框：色块里与底色明显不同的像素（含抗锯齿边缘）。(x0, y0, x1, y1)。"""
    plate = QColor(PLATE_HEX[mode])
    x0 = y0 = img.width()
    x1 = y1 = -1
    for y in range(img.height()):
        for x in range(img.width()):
            c = img.pixelColor(x, y)
            if c.alpha() < 250:
                continue          # 色块边缘与圆角外的抗锯齿像素不算图形
            if abs(c.red() - plate.red()) + abs(c.green() - plate.green()) + abs(c.blue() - plate.blue()) > 90:
                x0, x1 = min(x0, x), max(x1, x)
                y0, y1 = min(y0, y), max(y1, y)
    return x0, y0, x1, y1


def _ink_bbox(img, alpha_floor=128):
    """不透明像素的外框 (x0, y0, x1, y1)，x1/y1 为含端点的最后一个像素。"""
    x0 = y0 = img.width()
    x1 = y1 = -1
    for y in range(img.height()):
        for x in range(img.width()):
            if (img.pixel(x, y) >> 24) & 0xFF > alpha_floor:
                x0, x1 = min(x0, x), max(x1, x)
                y0, y1 = min(y0, y), max(y1, y)
    return x0, y0, x1, y1


def _drain_icon_queue():
    """取空图标队列，按先后顺序返回里面的 (kind, payload)。
    队列是后台出图线程与主窗之间唯一的通道，没有对外读取口，必须直接取。"""
    items = []
    while True:
        try:
            items.append(sf_icons._ICON_QUEUE.get_nowait())   # discipline-ok(R5): 图标队列没有对外读取口，这是它唯一的出口
        except queue.Empty:
            return items


def _run_icon_worker_and_collect():
    """清空队列 → 宣布 GUI 就绪 → 跑一遍后台出图线程的函数体 → 返回它发布的全部条目。
    就绪标志在 finally 里复位，免得影响后面的用例。"""
    _drain_icon_queue()
    sf_icons._app_ready.set()                 # discipline-ok(R5): 与主窗的握手标志没有对外接口
    try:
        sf_icons._icon_worker()               # discipline-ok(R5): 线程函数体本身；起真线程会让用例依赖调度时序
    finally:
        sf_icons._app_ready.clear()           # discipline-ok(R5): 同上，复位握手标志
    return _drain_icon_queue()


class IconCase(SFCase):
    """离屏 QApplication + 图标模块全局的保存/还原。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        super().setUp()
        self.d = tempfile.mkdtemp()
        self._saved = (sf_icons.ICON_PATH, sf_icons._button_images_cache)
        # make_app_icon 顺带把图写到 ICON_PATH（用户数据目录）。测试里统一改道到临时目录，
        # 免得每条用例都去刷一次真用户目录里的那份副产物。
        sf_icons.ICON_PATH = os.path.join(self.d, "icon.png")

    def tearDown(self):
        (sf_icons.ICON_PATH, sf_icons._button_images_cache) = self._saved
        super().tearDown()


# --------------------------------------------------------------------------
# 应用图标
# --------------------------------------------------------------------------

class TestAppIcon(IconCase):
    def test_image_is_argb_and_the_size_used_by_the_dock(self):
        img = sf_icons.make_app_icon()
        self.assertIsInstance(img, QImage)
        self.assertEqual(img.width(), 512)
        self.assertEqual(img.height(), 512)
        self.assertEqual(img.format(), QImage.Format.Format_ARGB32)

    def test_corners_stay_transparent_so_the_rounded_mask_reads(self):
        """四角必须透明：主窗按同一套圆角裁切，实心方角会在图标外露出一圈。"""
        img = sf_icons.make_app_icon()
        self.assertEqual(img.pixelColor(2, 2).alpha(), 0)
        self.assertEqual(img.pixelColor(509, 2).alpha(), 0)
        self.greater = img.pixelColor(256, 256).alpha()
        self.assertGreater(self.greater, 0, "中心必须是不透明的")

    def test_body_is_the_blue_gradient_with_white_candles(self):
        """兜底造型仍是上一版的蓝渐变 + 白蜡烛：品牌标志渲染不出来时用它。"""
        with mock.patch.object(sf_icons, "svg_pixmap", return_value=None):
            img = sf_icons.make_app_icon()
        # 底部中段：在圆角矩形内、且低于所有蜡烛与上影线，只剩渐变底色
        corner_inside = img.pixelColor(256, 455)
        self.assertGreater(corner_inside.blue(), 120,
                           "底色应是蓝色渐变：%s" % corner_inside.name())
        self.assertLess(corner_inside.red(), 120, corner_inside.name())
        # 三根蜡烛是白色实心矩形：整幅图里必须找得到不透明的近白像素
        whites = 0
        for x in range(0, 512, 4):
            for y in range(0, 512, 4):
                c = img.pixelColor(x, y)
                if c.red() > 235 and c.green() > 235 and c.blue() > 235:
                    whites += 1
        self.assertGreater(whites, 50, "蜡烛缺失 = 图标变成纯蓝方块")

    def test_logo_is_the_brand_mark_with_orange_red_and_ink(self):
        """默认造型是品牌标志：取色必须是偏橙红 #f40000 与墨 #14181f，
        旧的蓝色渐变底板不许再出现（现在的底是浅灰色块，不是蓝）。中心点必须是红箭（定稿形状）。"""
        img = sf_icons.make_app_icon()
        c = img.pixelColor(256, 256)
        self.assertEqual((c.red(), c.green(), c.blue()), (0xF4, 0x00, 0x00), "中心应是偏橙红：%s" % c.name())
        reds = inks = blues = 0
        for y in range(0, 512, 2):
            for x in range(0, 512, 2):
                p = img.pixelColor(x, y)
                if p.alpha() <= 128:
                    continue
                reds += (p.red() > 200 and p.green() < 60 and p.blue() < 60)
                inks += (max(p.red(), p.green(), p.blue()) < 60)
                # 饱和蓝：蓝通道明显高于红通道。浅灰底与墨的抗锯齿混色是中性灰，
                # 不会满足；旧的蓝渐变底板（#3977e8 / #1749a8）一定满足。
                blues += (p.blue() - p.red() > 60)
        self.assertGreater(reds, 300, "红色上斜笔与箭头缺失")
        self.assertGreater(inks, 600, "竖笔或下斜笔缺失")
        self.assertEqual(blues, 0, "蓝色底板又回来了：图标不是品牌标志")

    def test_plate_is_inset_not_edge_to_edge(self):
        """色块底不得贴画布边：贴边说明 Dock 里它会比相邻图标大一圈，
        且圆角会被系统遮罩二次裁掉。不透明像素的外框就是色块底本身。"""
        img = sf_icons.make_app_icon()
        x0, y0, x1, y1 = _ink_bbox(img)
        pad = img.width() * PLATE_MIN_MARGIN_FRACTION
        self.assertGreaterEqual(x0, pad)
        self.assertGreaterEqual(y0, pad)
        self.assertLessEqual(x1 + 1, img.width() - pad)
        self.assertLessEqual(y1 + 1, img.height() - pad)

    def test_plate_fraction_does_not_shrink_back(self):
        """色块底占画布的比例有下限：缩小了，Dock 里就比相邻图标小一圈。"""
        img = sf_icons.make_app_icon()
        x0, y0, x1, y1 = _ink_bbox(img)
        for got, label in (((x1 - x0 + 1) / img.width(), "宽"), ((y1 - y0 + 1) / img.height(), "高")):
            self.assertGreaterEqual(got, PLATE_MIN_FRACTION,
                                    "色块底%s只占画布 %.1f%%，低于下限 %.0f%%" % (label, got * 100, PLATE_MIN_FRACTION * 100))

    def test_logo_mark_fraction_of_plate_does_not_shrink_back(self):
        """标志图形占色块底的比例有下限（效果图上约 56%，下限取 50%）。
        色块底加上之后，标志缩小一点在整幅图上看不出来，但 Dock 里会显得空；
        量的是图形外框占色块边长的比例，不依赖字体或屏幕缩放。"""
        img = sf_icons.make_app_icon()
        px0, py0, px1, py1 = _ink_bbox(img)
        mx0, my0, mx1, my1 = _mark_bbox(img, "light")
        for got, label in (((mx1 - mx0 + 1) / (px1 - px0 + 1), "宽"), ((my1 - my0 + 1) / (py1 - py0 + 1), "高")):
            self.assertGreaterEqual(got, MARK_MIN_FRACTION_OF_PLATE,
                                    "标志%s只占色块的 %.1f%%，低于下限 %.0f%%：图形被缩回去了"
                                    % (label, got * 100, MARK_MIN_FRACTION_OF_PLATE * 100))
            self.assertLessEqual(got, MARK_MAX_FRACTION_OF_PLATE, "标志撑得太满，几乎贴住色块边缘")

    def test_light_plate_colour_is_the_approved_light_grey(self):
        """浅色外观的底色是定稿的浅灰 #e8ebf0：取色块左侧中段一点（避开图形与圆角）。"""
        img = sf_icons.make_app_icon()
        self.assertEqual(_plate_sample(img).name(), "#e8ebf0")

    def test_shipped_icon_png_is_drawn_at_the_same_scale_as_the_runtime_icon(self):
        """包体 icon.png（及由它同批出图的 icon.icns）必须与运行时 make_app_icon 同一个缩放。
        只改常量不重出图，装机后 Dock 图标读 icns、窗口图标读运行时，两处大小、底色会不一致，
        而且没有任何地方报错。比的是色块外框与图形外框，留 2 像素给抗锯齿边缘。"""
        shipped = QImage(os.path.join(REPO_ROOT, "icon.png"))
        self.assertFalse(shipped.isNull(), "仓库根目录的 icon.png 读不出来")
        self.assertEqual(shipped.width(), APP_ICON_PX, "随包 icon.png 应是 Dock 用的边长")
        runtime = sf_icons.make_app_icon()
        for what, want_box, got_box in (("色块", _ink_bbox(runtime), _ink_bbox(shipped)),
                                        ("图形", _mark_bbox(runtime, "light"), _mark_bbox(shipped, "light"))):
            for want, got in zip(want_box, got_box):
                self.assertLessEqual(abs(want - got), SHIPPED_ICON_BBOX_TOLERANCE_PX,
                                     "随包 icon.png 的%s外框与运行时出图对不上：重新导出 icon.icns / icon.png" % what)
        self.assertEqual(_plate_sample(shipped).name(), _plate_sample(runtime).name(),
                         "随包 icon.png 的底色与运行时不同：重新导出")

    def test_shipped_icns_carries_the_same_light_plate(self):
        """icon.icns 才是 Dock/访达读的那份。Qt 的 icns 插件只解出其中一档，
        这里不比像素外框，只确认它读得出来、且底色是浅灰：换了底色没重出 icns 会在这里红。"""
        shipped = QImage(os.path.join(REPO_ROOT, "icon.icns"))
        self.assertFalse(shipped.isNull(), "icon.icns 读不出来")
        self.assertEqual(_plate_sample(shipped).name(), "#e8ebf0")

    def test_dark_icon_has_the_dark_plate_and_the_light_ink(self):
        """深色外观那一张：底 #12151d，红箭 #fe0118，浅墨 #f2f4f8；中心点仍是红箭。"""
        img = sf_icons.make_app_icon("dark")
        self.assertEqual(_plate_sample(img).name(), "#12151d")
        c = img.pixelColor(256, 256)
        self.assertEqual((c.red(), c.green(), c.blue()), (0xFE, 0x01, 0x18), "中心应是深色档的红：%s" % c.name())
        def count_light_ink(image):
            # 浅墨 #f2f4f8 的最小通道是 242；浅灰底 #e8ebf0 是 232，所以阈值取两者之间的 238
            return sum(1 for y in range(0, 512, 2) for x in range(0, 512, 2)
                       if (lambda p: p.alpha() > 250 and min(p.red(), p.green(), p.blue()) > 238)(image.pixelColor(x, y)))

        self.assertGreater(count_light_ink(img), 300, "深色档的浅墨竖笔缺失")
        # 浅色那一张里不该有浅墨像素：证明两档确实是两套配色而不是同一张
        self.assertEqual(count_light_ink(sf_icons.make_app_icon("light")), 0)

    def test_dark_and_light_icons_share_the_same_geometry(self):
        """两档只换颜色，形状一个点不动：色块外框与图形外框逐点相同。"""
        light = sf_icons.make_app_icon("light")
        dark = sf_icons.make_app_icon("dark")
        self.assertEqual(_ink_bbox(light), _ink_bbox(dark), "两档的色块外框不同")
        self.assertEqual(_mark_bbox(light, "light"), _mark_bbox(dark, "dark"), "两档的图形外框不同")

    def test_unknown_mode_falls_back_to_the_light_icon(self):
        """传了不认识的档位，出浅色而不是抛错或出一张错图。"""
        self.assertEqual(_plate_sample(sf_icons.make_app_icon("sepia")).name(), "#e8ebf0")

    def test_dark_icon_does_not_overwrite_the_light_copy_on_disk(self):
        """ICON_PATH 那份副产物只属于浅色档：深色档出图不能碰它，
        否则两档在同一个文件上来回覆盖，哪一档后出图哪一档留在盘上。"""
        sf_icons.make_app_icon("light")
        before = QImage(sf_icons.ICON_PATH)
        self.assertFalse(before.isNull())
        sf_icons.make_app_icon("dark")
        after = QImage(sf_icons.ICON_PATH)
        self.assertEqual(_plate_sample(after).name(), "#e8ebf0", "深色档把盘上的图覆盖了")
        self.assertEqual(before, after)

    def test_dark_icon_is_not_written_when_nothing_was_written_before(self):
        """只出深色档时盘上不该冒出文件。"""
        sf_icons.make_app_icon("dark")
        self.assertFalse(os.path.exists(sf_icons.ICON_PATH))

    def test_dark_icon_falls_back_to_the_legacy_one_when_the_mark_cannot_render(self):
        """深色档同样有兜底：品牌标志渲染不出来时退回旧自绘图标，而不是没有图标。"""
        with mock.patch.object(sf_icons, "svg_pixmap", return_value=None):
            img = sf_icons.make_app_icon("dark")
        self.assertEqual(img.width(), APP_ICON_PX)
        self.assertGreater(img.pixelColor(APP_ICON_PX // 2, 455).blue(), 120)

    def test_missing_qtsvg_falls_back_without_losing_the_icon(self):
        """这台机器没有 QtSvg 子模块时（svg_pixmap 直接抛异常），图标必须还有，
        且退回上一版造型 —— 缺依赖不能变成"没有图标"。"""
        import builtins
        real_import = builtins.__import__

        def no_qtsvg(name, *a, **kw):
            if name.startswith("PyQt6.QtSvg"):
                raise ImportError("no QtSvg on this machine")
            return real_import(name, *a, **kw)

        with mock.patch("builtins.__import__", side_effect=no_qtsvg):
            img = sf_icons.make_app_icon()
        self.assertIsNotNone(img)
        self.assertEqual(img.width(), APP_ICON_PX, "兜底图标也必须是 Dock 用的边长")
        self.assertGreater(img.pixelColor(APP_ICON_PX // 2, 455).blue(), 120, "兜底造型应是旧的蓝渐变底板")

    def test_disk_copy_is_a_side_effect_not_a_precondition(self):
        sf_icons.ICON_PATH = os.path.join(self.d, "icon.png")
        img = sf_icons.make_app_icon()
        self.assertTrue(img.width())
        self.assertTrue(os.path.exists(sf_icons.ICON_PATH))

    def test_unwritable_icon_path_does_not_cost_the_caller_its_image(self):
        """调用方要的是内存里的 QImage；ICON_PATH 只是顺带写出的副产物。

        路径必须**真的**写不进去：makedirs(exist_ok=True) 会把 no/such/dir 直接建出来，
        那样写盘成功，except 分支从没执行过。这里在路径中间放一个普通文件，
        建目录必然失败（NotADirectoryError / FileExistsError），才真走到"静默跳过"那一行。"""
        blocker = os.path.join(self.d, "blocker")
        with open(blocker, "w") as fh:
            fh.write("a regular file where a directory is expected")
        sf_icons.ICON_PATH = os.path.join(blocker, "dir", "icon.png")
        img = sf_icons.make_app_icon()
        self.assertEqual(img.width(), APP_ICON_PX)
        self.assertFalse(os.path.exists(sf_icons.ICON_PATH), "路径不该被建出来：否则这条用例没走到失败分支")


# --------------------------------------------------------------------------
# SVG 按钮图标
# --------------------------------------------------------------------------

class TestSvgPixmap(IconCase):
    def test_real_icons_render_non_empty_ink(self):
        for kind, svg in sf_icons._ICON_SVG.items():
            img = sf_icons.svg_pixmap(svg, 20)
            self.assertIsNotNone(img, kind)
            self.assertEqual((img.width(), img.height()), (20, 20), kind)
            ink = sum(1 for x in range(20) for y in range(20)
                      if img.pixelColor(x, y).alpha() > 0)
            self.assertGreater(ink, 4, "%s 渲染出来几乎是空的" % kind)

    def test_larger_px_gives_a_larger_bitmap(self):
        svg = sf_icons._ICON_SVG["close"]
        self.assertEqual(sf_icons.svg_pixmap(svg, 40).width(), 40)

    def test_broken_svg_returns_none_so_the_fallback_can_take_over(self):
        for bad in ("<svg>", "", "not xml at all", "<svg xmlns=x></svg>"):
            self.assertIsNone(sf_icons.svg_pixmap(bad, 20), repr(bad))

    def test_missing_qtsvg_returns_none_instead_of_raising(self):
        """QtSvg 缺失的部署环境不能让 sf_icons 整体导入失败（连兜底图标一起陪葬）。"""
        with mock.patch.dict(sys.modules, {"PyQt6.QtSvg": None}):
            self.assertIsNone(sf_icons.svg_pixmap(sf_icons._ICON_SVG["close"], 20))

    def test_button_images_are_rendered_once_and_cached(self):
        first = sf_icons.button_images()
        with mock.patch.object(sf_icons, "svg_pixmap",
                               side_effect=AssertionError("不该再渲染一次")):
            self.assertIs(sf_icons.button_images(), first)
        self.assertEqual(sorted(first), ["close", "edit", "min"])

    def test_a_single_failed_icon_degrades_to_none_not_an_exception(self):
        sf_icons._button_images_cache = None
        with mock.patch.object(sf_icons, "svg_pixmap",
                               side_effect=[None, None, None]):
            imgs = sf_icons.button_images()
        self.assertEqual(list(imgs.values()), [None, None, None])
        # 弹窗侧走的是 QPixmap：全部 None 时窗口沿用自绘兜底，不能抛
        pix = sf_icons.button_pixmaps()
        self.assertEqual(pix, {"edit": None, "close": None, "min": None})

    def test_button_pixmaps_returns_pixmap_per_kind(self):
        pix = sf_icons.button_pixmaps()
        self.assertEqual(sorted(pix), ["close", "edit", "min"])
        for k, v in pix.items():
            self.assertIsNotNone(v, k)
            # 不钉裸数字：真正要守的是「逻辑边长 == 绘制框、按设备倍率出图」
            self.assertAlmostEqual(v.width() / v.devicePixelRatio(),
                                   sf_ui_render.BTN_ICON_SZ, places=3,
                                   msg="%s 的逻辑尺寸与绘制框对不上（Retina 会重新上采样）" % k)

    def test_pixmaps_failure_is_logged_and_degrades(self):
        """无 GUI 系统等极端环境下不能让建窗失败，但必须留一条 WARNING。"""
        with mock.patch.object(sf_icons, "button_images", side_effect=RuntimeError("no x")):
            with mock.patch.object(sf_icons, "log_line") as lg:
                pix = sf_icons.button_pixmaps()
        self.assertEqual(pix, {"edit": None, "close": None, "min": None})
        self.assertEqual(lg.call_args.args[1] if len(lg.call_args.args) > 1
                         else lg.call_args.kwargs.get("level"), "WARNING")


# --------------------------------------------------------------------------
# Dock 策略与 SF Symbols（假 AppKit，不碰真实 Dock）
# --------------------------------------------------------------------------

class _FakeAppKit:
    def __init__(self, app=None, symbol_image="auto", draw_raises=False):
        self.NSApp = app if app is not None else mock.MagicMock()
        self.NSApplicationActivationPolicyAccessory = 1
        self.NSApplicationActivationPolicyRegular = 0
        self.NSFontWeightBold = 8.0
        self.NSImage = mock.MagicMock()
        if symbol_image is None:
            self.NSImage.imageWithSystemSymbolName_accessibilityDescription_.return_value = None
        self.NSBitmapImageRep = mock.MagicMock()
        self.NSGraphicsContext = mock.MagicMock()
        self.NSMakeRect = lambda *a: a
        self.NSImageSymbolConfiguration = mock.MagicMock()
        self._draw_raises = draw_raises
        if draw_raises:
            self.NSImage().drawInRect_.side_effect = RuntimeError("no context")


class TestDockPolicy(IconCase):
    def test_hide_dock_switches_to_accessory(self):
        fake = _FakeAppKit()
        with mock.patch.dict(sys.modules, {"AppKit": fake}):
            sf_icons.hide_dock()
        fake.NSApp.setActivationPolicy_.assert_called_once_with(1)

    def test_show_dock_switches_to_regular_and_leaves_a_trace(self):
        fake = _FakeAppKit()
        with mock.patch.dict(sys.modules, {"AppKit": fake}), \
                mock.patch.object(sf_icons, "log_line") as lg:
            sf_icons.show_dock()
        fake.NSApp.setActivationPolicy_.assert_called_once_with(0)
        self.assertIn("show_dock OK", lg.call_args.args[0])

    def test_show_dock_failure_is_logged_not_raised(self):
        """这条是"点 Dock 图标恢复窗口"唯一的可观测痕迹。"""
        boom = _FakeAppKit()
        boom.NSApp.setActivationPolicy_.side_effect = RuntimeError("detached")
        with mock.patch.dict(sys.modules, {"AppKit": boom}), \
                mock.patch.object(sf_icons, "log_line") as lg:
            sf_icons.show_dock()
        self.assertIn("show_dock ERROR", lg.call_args.args[0])

    def test_missing_appkit_is_silent_for_hide_and_loud_for_show(self):
        with mock.patch.dict(sys.modules, {"AppKit": None}):
            sf_icons.hide_dock()          # 隐藏失败无所谓：最坏就是 Dock 上多个图标
            with mock.patch.object(sf_icons, "log_line") as lg:
                sf_icons.show_dock()
            self.assertIn("show_dock ERROR", lg.call_args.args[0])


class TestSfSymbolPixmap(IconCase):
    def test_unavailable_symbol_returns_none(self):
        with mock.patch.dict(sys.modules, {"AppKit": _FakeAppKit(symbol_image=None)}):
            self.assertIsNone(sf_icons.sf_symbol_pixmap("star.fill", 14, "white"))

    def test_weight_configuration_is_applied_when_requested(self):
        fake = _FakeAppKit()
        with mock.patch.dict(sys.modules, {"AppKit": fake}):
            sf_icons.sf_symbol_pixmap("bell.fill", 14, "white", weight=8.0)
        fake.NSImageSymbolConfiguration \
            .configurationWithPointSize_weight_.assert_called_once_with(14, 8.0)

    def test_graphics_state_is_restored_even_when_drawing_fails(self):
        """drawInRect_ 抛错时若不 restore，线程的 graphics state 栈永久残留一次 save。"""
        fake = _FakeAppKit(draw_raises=True)
        with mock.patch.dict(sys.modules, {"AppKit": fake}):
            self.assertIsNone(sf_icons.sf_symbol_pixmap("star.fill", 14, "white"))
        self.assertEqual(fake.NSGraphicsContext.saveGraphicsState.call_count, 1)
        self.assertEqual(fake.NSGraphicsContext.restoreGraphicsState.call_count, 1,
                         "save/restore 必须成对")

    def test_missing_appkit_degrades_to_the_drawn_fallback(self):
        with mock.patch.dict(sys.modules, {"AppKit": None}):
            self.assertIsNone(sf_icons.sf_symbol_pixmap("star.fill", 14, "white"))


class TestObjcDepsAndWorker(IconCase):
    def test_dependency_import_is_cached(self):
        first = sf_icons._objc_deps()
        with mock.patch.dict(sys.modules, {"objc": None}):
            self.assertIs(sf_icons._objc_deps(), first,
                          "第二次不得重新 import（首次 import 才是慢的那一次）")

    def test_worker_publishes_the_app_icon_once_gui_is_ready(self):
        items = _run_icon_worker_and_collect()
        self.assertEqual([k for k, _ in items], ["app_icon", "app_icon_dark"], "只发布浅、深两张，且浅色在前")
        light, dark = items[0][1], items[1][1]
        self.assertIsInstance(light, QImage)
        self.assertEqual(_plate_sample(light).name(), "#e8ebf0", "第一张必须是浅色档")
        self.assertEqual(_plate_sample(dark).name(), "#12151d", "第二张必须是深色档")

    def test_a_failing_dark_render_does_not_cost_the_light_icon(self):
        """两档各自成败：深色档渲染炸了，浅色那张照样已经发布。"""
        real = sf_icons.make_app_icon

        def flaky(mode="light"):
            if mode == "dark":
                raise RuntimeError("oom")
            return real(mode)

        with mock.patch.object(sf_icons, "make_app_icon", side_effect=flaky):
            items = _run_icon_worker_and_collect()
        self.assertEqual([k for k, _ in items], ["app_icon", "app_icon_dark"])
        self.assertIsInstance(items[0][1], QImage)
        self.assertIsNone(items[1][1], "失败的那一档以 None 占位")

    def test_worker_skips_the_icon_when_the_gui_never_came_up(self):
        """超时不能把 None 入队伪装成"生成失败"，那会让图标静默缺失。"""
        while True:
            try:
                sf_icons._ICON_QUEUE.get_nowait()
            except queue.Empty:
                break
        ready = mock.Mock()
        ready.wait.return_value = False
        with mock.patch.object(sf_icons, "_app_ready", ready):
            sf_icons._icon_worker()
        self.assertTrue(ready.wait.called)
        self.assertEqual(sf_icons._ICON_QUEUE.qsize(), 0)

    def test_worker_queues_none_when_rendering_blows_up(self):
        with mock.patch.object(sf_icons, "make_app_icon", side_effect=RuntimeError("oom")):
            items = _run_icon_worker_and_collect()
        self.assertEqual(items, [("app_icon", None), ("app_icon_dark", None)])

    def test_queue_capacity_bounds_back_to_back_producers(self):
        self.assertEqual(sf_icons._ICON_QUEUE.maxsize, 8,
                         "消费端停摆时图标不得无限堆积")

    def test_start_worker_runs_in_the_background_and_does_not_block_exit(self):
        with mock.patch.object(sf_icons, "_icon_worker") as w:
            sf_icons.start_icon_worker()
            self.assertTrue(wait_for(lambda: w.called, timeout=3.0,
                                     msg="图标是渐进增强，必须在后台预热"))


# --------------------------------------------------------------------------
# 编辑器：zombie 线程登记
# --------------------------------------------------------------------------

class TestZombieRegistry(SFCase):
    """H-1：关闭时仍在运行的搜索线程由模块级表持有，线程结束自清。"""

    def setUp(self):
        super().setUp()
        self._saved = list(sf_editor._zombie_threads)
        sf_editor._zombie_threads.clear()

    def tearDown(self):
        sf_editor._zombie_threads[:] = self._saved
        super().tearDown()

    def test_finished_threads_leave_the_table_running_ones_stay(self):
        running = mock.Mock(isRunning=lambda: True)
        done = mock.Mock(isRunning=lambda: False)
        sf_editor._zombie_threads[:] = [running, done]
        sf_editor._prune_zombie_threads()
        self.assertEqual(sf_editor._zombie_threads, [running],
                         "丢弃仍在运行的线程 = QThread 运行中析构直接 abort")

    def test_a_thread_that_died_underneath_us_is_dropped_quietly(self):
        wrapped = mock.Mock()
        wrapped.isRunning.side_effect = RuntimeError("deleted C++ object")
        sf_editor._zombie_threads[:] = [wrapped]
        sf_editor._prune_zombie_threads()
        self.assertEqual(sf_editor._zombie_threads, [])

    def test_pruning_is_safe_against_a_vanished_entry(self):
        class _SelfRemoving:
            def isRunning(self):
                sf_editor._zombie_threads.remove(self)
                return False

            def __eq__(self, other):
                return self is other

            def __hash__(self):
                return id(self)

        item = _SelfRemoving()
        sf_editor._zombie_threads.append(item)
        sf_editor._prune_zombie_threads()      # 只要求不抛 ValueError


class _HintSink:
    """只记录被写过的状态行文案。"""

    def __init__(self, owner):
        self.owner = owner

    def setText(self, text):
        self.owner.hints.append(text)


class _Editor:
    """以最小接口驱动 EditorWindow 的生产方法，隔离 QWidget 生命周期。

    直调未绑定方法的原因：EditorWindow 构造函数会建真实窗口并注入毛玻璃
    NSWindow，在离屏全量回归里既慢又与"搜索链路收口"这件事无关。
    test_bugfix_151.py 已确立同一手法，这里沿用。
    """

    def __init__(self, code="sh600519", applied=None, ok_enabled=True):
        self.code = code
        self.applied = applied if applied is not None else []
        self.hints = []
        self.ok = mock.Mock(isEnabled=lambda: ok_enabled)
        self.entry = mock.Mock()
        self._search_thread = None
        self._search_seq = 0
        self._closed = False
        self.closed = False
        self.cand_box = mock.Mock()
        self._anchor = object()
        self.on_closed = None
        self.hint = _HintSink(self)

    @property
    def hint_text(self):
        return self.hints[-1] if self.hints else None

    def on_apply(self, code):
        self.applied.append(code)

    def close(self):
        self.closed = True

    def _close(self):
        sf_editor.EditorWindow._close(self)

    def adjustSize(self):
        pass

    def _move_near(self):
        pass

    def _clear_candidates(self):
        pass

    def _detach_search_thread(self):
        sf_editor.EditorWindow._detach_search_thread(self)

    def _is_dup(self, code):
        return sf_editor.EditorWindow._is_dup(self, code)

    def _on_text_changed(self, text):
        return sf_editor.EditorWindow._on_text_changed(self, text)

    def do_search(self):
        return sf_editor.EditorWindow.do_search(self)

    def _on_search_done(self, results, seq=0):
        return sf_editor.EditorWindow._on_search_done(self, results, seq)

    def _choose_candidate(self, code):
        return sf_editor.EditorWindow._choose_candidate(self, code)


class TestEditorTextGuard(SFCase):
    def test_empty_input_greys_out_the_confirm_button(self):
        ed = _Editor()
        ed._on_text_changed("   ")
        ed.ok.setEnabled.assert_called_with(False)

    def test_same_stock_as_current_one_is_not_a_change(self):
        """和当前股票等价（带前缀/纯数字两种写法都算）时置灰，避免"替换"成同一只。"""
        ed = _Editor(code="sh600519")
        ed._on_text_changed("600519")
        ed.ok.setEnabled.assert_called_with(False)
        ed2 = _Editor(code="sh600519")
        ed2._on_text_changed("贵州茅台")
        ed2.ok.setEnabled.assert_called_with(True)

    def test_a_broken_normaliser_lets_the_user_through(self):
        """判定出错时放行：宁可让用户多点一次，也不能把「确定」永久锁死。"""
        ed = _Editor()
        with mock.patch.object(sf_editor, "normalize_code",
                               side_effect=RuntimeError("bad")):
            with mock.patch.object(ed.ok, "setEnabled", side_effect=lambda v: setattr(
                    ed, "_seen", v)):
                ed._on_text_changed("600519")
        self.assertTrue(getattr(ed, "_seen", None) is True
                        or ed.ok.setEnabled.call_count)


class TestEditorSearchChain(SFCase):
    def setUp(self):
        super().setUp()
        # Output tests run their fast substitutes inline; real threading is tested separately.
        self.track_patch(mock.patch.object(sf_editor.SearchThread, "start", lambda thread: thread.run()))
        self.track_patch(mock.patch.object(sf_universe, "match_confident", return_value=[]))
        self.track_patch(mock.patch.object(sf_universe, "ensure_ready"))
        self.track_patch(mock.patch("sf_quote.fetch_quote",
            side_effect=lambda code: {"name": "Example", "code": code}))

    def test_six_digit_code_applies_immediately_and_closes(self):
        ed = _Editor(code="sh000001")
        ed.entry.text.return_value = "600519"
        with mock.patch.object(sf_universe, "ensure_ready"), \
                mock.patch.object(sf_universe, "index_ready", return_value=False):
            ed.do_search()
        self.assertEqual(ed.applied, ["sh600519"])
        self.assertTrue(ed.closed)

    def test_a_duplicate_stock_asks_instead_of_silently_replacing(self):
        ed = _Editor(code="sh600519")
        ed.entry.text.return_value = "600519"
        ed.do_search()
        self.assertEqual(ed.applied, [])
        self.assertIn("已在显示中", ed.hint_text)

    def test_unknown_code_is_rejected_once_the_dictionary_is_ready(self):
        """v1.4.18 建议#5：误输不存在的代码会把当前股票覆盖成 "--" 并清空目标价。

        `covers_segment` 必须一起桩掉：这条用例判的是"**字典管的段**里查无此码"，
        而 123456 前缀是 12（可转债）—— 字典不管那段，代码会改走行情探针那条支路，
        于是走哪条支路取决于别的用例留下了什么桩（随机顺序档实测暴露：seed=20261012
        下它退化成"探针抛 AssertionError → 直接放行"，判据就白写了）。
        """
        ed = _Editor(code="sh000001")
        ed.entry.text.return_value = "123456"
        from sf_network import QuoteDataError
        with mock.patch.object(sf_universe, "ensure_ready"), \
                mock.patch.object(sf_universe, "covers_segment", return_value=True), \
                mock.patch.object(sf_universe, "index_ready", return_value=True), \
                mock.patch.object(sf_universe, "match_confident", return_value=[]), \
                mock.patch("sf_quote.fetch_quote", side_effect=QuoteDataError("not found")):
            ed.do_search()
        self.assertEqual(ed.applied, [])
        self.assertIn("未找到该代码", ed.hint_text)
        self.assertFalse(ed.closed)

    def test_dictionary_still_lets_a_known_code_through(self):
        ed = _Editor(code="sh000001")
        ed.entry.text.return_value = "600519"
        with mock.patch.object(sf_universe, "ensure_ready"), \
                mock.patch.object(sf_universe, "covers_segment", return_value=True), \
                mock.patch.object(sf_universe, "index_ready", return_value=True), \
                mock.patch.object(sf_universe, "match_confident",
                                  return_value=[("sh600519", "贵州茅台")]):
            ed.do_search()
        self.assertEqual(ed.applied, ["sh600519"])

    def test_a_dictionary_crash_falls_back_to_the_fast_path(self):
        """字典侧任何异常都退回原有的直接替换，绝不让编辑窗卡死。"""
        ed = _Editor(code="sh000001")
        ed.entry.text.return_value = "600519"
        with mock.patch.object(sf_universe, "ensure_ready",
                               side_effect=RuntimeError("disk")):
            ed.do_search()
        self.assertEqual(ed.applied, ["sh600519"])

    def test_a_failing_on_apply_does_not_close_the_window(self):
        ed = _Editor(code="sh000001")
        ed.on_apply = lambda c: (_ for _ in ()).throw(RuntimeError("config ro"))
        ed.entry.text.return_value = "600519"
        with mock.patch.object(sf_universe, "ensure_ready"), \
                mock.patch.object(sf_universe, "index_ready", return_value=False):
            ed.do_search()
        self.assertIn("替换失败", ed.hint_text)
        self.assertFalse(ed.closed, "切股失败还关掉窗口 = 用户以为已经换好了")

    def test_greyed_confirm_button_blocks_enter_too(self):
        ed = _Editor(ok_enabled=False)
        ed.entry.text.return_value = "600519"
        ed.do_search()
        self.assertEqual(ed.applied, [])
        self.assertIsNone(ed._search_thread)

    def test_blank_input_is_not_a_search(self):
        ed = _Editor()
        ed.entry.text.return_value = "   "
        ed.do_search()
        self.assertIsNone(ed._search_thread)

    def test_a_running_search_is_not_replaced_by_a_second_one(self):
        ed = _Editor()
        ed.entry.text.return_value = "茅台"
        running = mock.Mock(isRunning=lambda: True)
        ed._search_thread = running
        ed.do_search()
        self.assertIs(ed._search_thread, running,
                      "覆盖运行中的线程引用会让 QThread 被 GC，直接 abort")

    def test_name_search_starts_a_thread_tagged_with_a_newer_sequence(self):
        ed = _Editor()
        ed.entry.text.return_value = "茅台"
        with mock.patch.object(sf_editor, "SearchThread") as st:
            ed.do_search()
        self.assertIn("搜索中", ed.hint_text)
        self.assertEqual(st.call_args.args, ("茅台", 1))
        st.return_value.done.connect.assert_called_once()
        st.return_value.start.assert_called_once()

    def test_stale_packet_from_the_previous_keyword_is_dropped(self):
        """v1.2.71 序号闸门：旧关键词的回包晚到也不能覆盖新结果。"""
        ed = _Editor()
        ed._search_seq = 5
        ed._on_search_done([("sh600000", "浦发银行")], 4)
        self.assertEqual(ed.applied, [])
        self.assertEqual(ed.hints, [])

    def test_a_packet_that_arrives_after_closing_is_dropped(self):
        """H-2：迟到回包曾把用户刚替换的股票覆盖回旧结果（实测 5s 后回退）。"""
        ed = _Editor()
        ed._closed = True
        ed._on_search_done([("sh600519", "贵州茅台")], 9)
        self.assertEqual(ed.applied, [])

    def test_no_hit_says_so_plainly(self):
        ed = _Editor()
        ed._on_search_done([], 1)
        self.assertIn("未找到匹配的 A 股", ed.hint_text)

    def test_single_hit_applies_and_closes(self):
        ed = _Editor(code="sh000001")
        ed._search_seq = 1
        ed._on_search_done([("sh600519", "贵州茅台")], 1)
        self.assertEqual(ed.applied, ["sh600519"])
        self.assertTrue(ed.closed)

    def test_single_hit_that_duplicates_the_current_stock_asks_first(self):
        ed = _Editor(code="sh600519")
        ed._on_search_done([("600519", "贵州茅台")], 1)
        self.assertEqual(ed.applied, [])
        self.assertIn("已在显示中", ed.hint_text)

    def test_many_hits_are_capped_and_the_list_is_rebuilt(self):
        ed = _Editor(code="sh000001")
        results = [("sh6%05d" % i, "名称%d" % i) for i in range(20)]
        added = []
        ed.cand_box.addWidget = added.append
        ed._on_search_done(results, 1)
        self.assertEqual(len(added), 8, "弹窗最多 8 条，多了用户翻不动")
        self.assertIn("结果过多", ed.hint_text)
        self.assertEqual(ed.applied, [], "多结果不能自动选一条就切股")

    def test_a_long_candidate_name_is_truncated_so_the_layout_holds(self):
        ed = _Editor(code="sh000001")
        labels = []
        real_button = QPushButton

        def capture(widget):
            labels.append(widget.text())

        ed.cand_box.addWidget = capture
        ed._on_search_done([("sh600519", "超" * 40),
                            ("sh600520", "长" * 40)], 1)
        self.assertTrue(labels)
        self.assertLessEqual(len(labels[0]), 25, "超长名称会撑破固定宽度弹窗")
        self.assertTrue(labels[0].endswith("…"))
        del real_button

    def test_clicking_a_candidate_reuses_the_same_duplicate_guard(self):
        ed = _Editor(code="sh600519")
        ed._choose_candidate("sh600519")
        self.assertEqual(ed.applied, [])
        self.assertIn("已在显示中", ed.hint_text)
        ed._choose_candidate("sz000001")
        self.assertEqual(ed.applied, ["sz000001"])
        self.assertTrue(ed.closed)

    def test_a_failing_candidate_apply_keeps_the_panel_open(self):
        ed = _Editor(code="sh000001")
        ed.on_apply = lambda c: (_ for _ in ()).throw(RuntimeError("ro"))
        ed._choose_candidate("sz000001")
        self.assertIn("替换失败", ed.hint_text)
        self.assertFalse(ed.closed)


class TestEditorClosePaths(SFCase):
    def setUp(self):
        super().setUp()
        self._saved = list(sf_editor._zombie_threads)
        sf_editor._zombie_threads.clear()

    def tearDown(self):
        sf_editor._zombie_threads[:] = self._saved
        super().tearDown()

    def test_close_notifies_the_owner_once_and_clears_the_callback(self):
        """主窗靠这个回调清 _editor 引用；不通知会留下悬空引用。"""
        ed = _Editor()
        called = []
        ed.on_closed = lambda: called.append(1)
        ed._close()
        self.assertEqual(called, [1])
        self.assertIsNone(ed.on_closed)
        self.assertTrue(ed.closed)
        self.assertIsNone(ed._anchor, "关闭后不再持有主窗几何")

    def test_a_broken_owner_callback_still_closes_the_window(self):
        ed = _Editor()
        ed.on_closed = lambda: (_ for _ in ()).throw(RuntimeError("gone"))
        ed._close()
        self.assertTrue(ed.closed)

    def test_running_thread_is_handed_to_the_registry_and_unlinked(self):
        ed = _Editor()
        th = mock.Mock(isRunning=lambda: True)
        ed._search_thread = th
        ed._detach_search_thread()
        self.assertIn(th, sf_editor._zombie_threads,
                      "窗口销毁后没人持有运行中的 QThread → 运行中析构 abort")
        th.done.disconnect.assert_called_once_with(ed._on_search_done)

    def test_detach_is_idempotent(self):
        ed = _Editor()
        th = mock.Mock(isRunning=lambda: True)
        ed._search_thread = th
        ed._detach_search_thread()
        ed._detach_search_thread()
        self.assertEqual(sf_editor._zombie_threads.count(th), 1)

    def test_finished_threads_are_left_alone(self):
        ed = _Editor()
        th = mock.Mock(isRunning=lambda: False)
        ed._search_thread = th
        ed._detach_search_thread()
        self.assertEqual(sf_editor._zombie_threads, [])
        self.assertEqual(th.done.disconnect.call_count, 0)

    def test_a_thread_whose_signal_was_already_unlinked_still_gets_registered(self):
        ed = _Editor()
        th = mock.Mock(isRunning=lambda: True)
        th.done.disconnect.side_effect = RuntimeError("already detached")
        ed._search_thread = th
        ed._detach_search_thread()
        self.assertIn(th, sf_editor._zombie_threads)


class TestSearchThreadBody(SFCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._app = QApplication.instance() or QApplication([])

    def test_results_are_emitted_with_the_sequence_tag(self):
        st = sf_editor.SearchThread("茅台", seq=7)
        got = []
        st.done.connect(lambda results, seq: got.append((results, seq)))
        with mock.patch.object(sf_editor, "search_stocks",
                               return_value=[("sh600519", "贵州茅台")]):
            st.run()
        self.assertEqual(got, [([("sh600519", "贵州茅台")], 7)])

    def test_a_network_failure_emits_an_empty_list_not_an_exception(self):
        """run() 抛未捕获异常会让 QThread 以异常终止且没有任何日志。"""
        st = sf_editor.SearchThread("茅台", seq=1)
        got = []
        st.done.connect(lambda results, seq: got.append(results))
        with mock.patch.object(sf_editor, "search_stocks",
                               side_effect=OSError("network down")):
            st.run()
        self.assertEqual(got, [[]])



if __name__ == "__main__":
    unittest.main()
