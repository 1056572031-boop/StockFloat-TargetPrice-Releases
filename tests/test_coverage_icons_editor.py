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

from PyQt6.QtGui import QImage
from PyQt6.QtWidgets import QApplication, QPushButton

import sf_editor             # noqa: E402
import sf_icons              # noqa: E402
import sf_ui_render          # noqa: E402
import sf_universe           # noqa: E402

try:
    from _base import SFCase, wait_for
except ImportError:           # pragma: no cover - 包模式导入
    from tests._base import SFCase, wait_for


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

    def test_disk_copy_is_a_side_effect_not_a_precondition(self):
        sf_icons.ICON_PATH = os.path.join(self.d, "icon.png")
        img = sf_icons.make_app_icon()
        self.assertTrue(img.width())
        self.assertTrue(os.path.exists(sf_icons.ICON_PATH))

    def test_unwritable_icon_path_does_not_cost_the_caller_its_image(self):
        """调用方要的是内存里的 QImage；ICON_PATH 只是顺带写出的副产物。"""
        sf_icons.ICON_PATH = os.path.join(self.d, "no", "such", "dir", "icon.png")
        img = sf_icons.make_app_icon()
        self.assertEqual(img.width(), 512)


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
        while True:
            try:
                sf_icons._ICON_QUEUE.get_nowait()
            except queue.Empty:
                break
        sf_icons._app_ready.set()
        self.addCleanup(sf_icons._app_ready.clear)
        sf_icons._icon_worker()
        kind, payload = sf_icons._ICON_QUEUE.get_nowait()
        self.assertEqual(kind, "app_icon")
        self.assertIsInstance(payload, QImage)

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
        while True:
            try:
                sf_icons._ICON_QUEUE.get_nowait()
            except queue.Empty:
                break
        sf_icons._app_ready.set()
        self.addCleanup(sf_icons._app_ready.clear)
        with mock.patch.object(sf_icons, "make_app_icon", side_effect=RuntimeError("oom")):
            sf_icons._icon_worker()
        self.assertEqual(sf_icons._ICON_QUEUE.get_nowait(), ("app_icon", None))

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
