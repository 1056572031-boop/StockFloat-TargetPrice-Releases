"""Exercise native layout geometry without allocating AppKit windows.

Real Cocoa rendering remains covered by the existing subprocess harness. These
tests isolate the geometry algorithm and assert observable frames and alignment,
including text measurement failure and window size constraints.
"""
from __future__ import annotations

import pathlib
import sys
import types
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from _base import SFCase
except ImportError:  # pragma: no cover
    from tests._base import SFCase

import sf_system_alert as alerts

# Layout helpers have no public entry point independent of NSAlert allocation.
center_content = alerts.NativeAlert._center_content  # discipline-ok(R5): geometry algorithm under test
text_height = alerts.NativeAlert._text_height  # discipline-ok(R5): measurement fallback under test
grow_window = alerts.NativeAlert._grow_window  # discipline-ok(R5): window geometry helper under test
hide_button_band = alerts.NativeAlert._hide_button_band  # discipline-ok(R5): button-less popup geometry under test

CONTENT_WIDTH = 520.0
CONTENT_HEIGHT = 310.0
ICON_SIZE = 64.0
SIDE_MARGIN = 20.0
BOTTOM_GAP = 20.0
BODY_Y = 126.0
BUTTON_Y = 16.0
BUTTON_HEIGHT = 32.0
BODY_HEIGHT = 90.0
LONG_BODY_MEASURE = 160.0
TEXT_CLEARANCE = 2.0
ROOMY_HEIGHT = 600.0
MAX_GROWTH = 480.0
HUGE_TEXT_HEIGHT = 10000.0
MEASUREMENT_FRAME_HEIGHT = 12.0
MEASUREMENT_CELL_HEIGHT = 25.0
MEASUREMENT_BOX_WIDTH = 200.0
EMPTY_HEIGHT = 0.0


def rect(x, y, width, height):
    return types.SimpleNamespace(origin=types.SimpleNamespace(x=x, y=y),
                                 size=types.SimpleNamespace(width=width, height=height))


class View:
    def __init__(self, frame):
        self.geometry = frame
        self.writes = []
        self.hidden = False

    def frame(self):
        return self.geometry

    def setFrame_(self, frame):
        self.geometry = frame
        self.writes.append(frame)

    # 真实 NSView 都有这一对；产品代码按它判断「按钮区是否被藏起来」。
    def isHidden(self):
        return self.hidden

    def setHidden_(self, hidden):
        self.hidden = bool(hidden)


class ImageView(View):
    pass


class Button(View):
    """NSAlert 按钮：隐藏按钮区时产品会逐个禁用并清掉回车/Esc 键位。"""

    def __init__(self, frame):
        super().__init__(frame)
        self.enabled = True
        self.key = "\r"

    def setEnabled_(self, enabled):
        self.enabled = bool(enabled)

    def setKeyEquivalent_(self, key):
        self.key = key


# NSAlert 的文案是带属性的字符串，里面的段落样式压过控件与 cell 的对齐设置（左对齐的真弹框读回：
# 两侧留白差 17~366 像素）。下面几个替身只模拟产品代码用到的那几个入口，并记录写入。
NATURAL_ALIGNMENT = 4          # 系统默认写在文案里的"自然对齐"
CENTER_ALIGNMENT = 1           # 与真实 NSTextAlignmentCenter 同值
WRAP_BY_WORD = 0
TRUNCATE_TAIL = 4


class ParaStyle:
    def __init__(self, align=NATURAL_ALIGNMENT, line_break=WRAP_BY_WORD):
        self.align = align
        self.line_break = line_break

    def mutableCopy(self):
        return ParaStyle(self.align, self.line_break)

    def setAlignment_(self, alignment):
        self.align = alignment

    def setLineBreakMode_(self, mode):
        self.line_break = mode

    def alignment(self):
        return self.align

    def lineBreakMode(self):
        return self.line_break


class AttrString:
    """带一个段落样式属性的字符串。style 为 None 表示文案里没有段落样式。"""

    def __init__(self, text, style):
        self.text = text
        self.style = style

    def length(self):
        return len(self.text)

    def attribute_atIndex_effectiveRange_(self, name, index, effective_range):
        # PyObjC 对带输出参数的方法返回 (值, 范围) 元组
        return (self.style, (0, len(self.text)))

    def initWithAttributedString_(self, other):
        self.text, self.style = other.text, other.style
        return self

    def addAttribute_value_range_(self, name, value, span):
        self.style = value


class AttrStringFactory:
    @staticmethod
    def alloc():
        return AttrString("", None)


class ParaStyleFactory:
    @staticmethod
    def alloc():
        return types.SimpleNamespace(init=ParaStyle)


class TextField(View):
    def __init__(self, frame, text="text", needed_height=20.0, has_cell=True):
        super().__init__(frame)
        self.text = text
        self.needed_height = needed_height
        self.has_cell = has_cell
        self.alignment = None
        self.cell_alignment = None
        self.paragraph = ParaStyle()          # 文案里原本带的段落样式；设成 None 模拟"没有"
        self.attributed_error = None          # 设成异常实例，模拟取带属性字符串失败
        self.attributed_written = None        # 产品代码写回去的带属性字符串
        self.measurements = []
        self.cell_object = types.SimpleNamespace(
            cellSizeForBounds_=self.measure,
            setAlignment_=self.align_cell)

    def stringValue(self):
        return self.text

    def attributedStringValue(self):
        if self.attributed_error is not None:
            raise self.attributed_error
        return AttrString("" if self.text == "" else self.text, self.paragraph)

    def setAttributedStringValue_(self, attributed):
        self.attributed_written = attributed

    def setAlignment_(self, alignment):
        self.alignment = alignment

    def align_cell(self, alignment):
        self.cell_alignment = alignment

    def cell(self):
        return self.cell_object if self.has_cell else None

    def measure(self, frame):
        self.measurements.append(frame)
        if isinstance(self.needed_height, Exception):
            raise self.needed_height
        return types.SimpleNamespace(height=self.needed_height)


class Content(View):
    def __init__(self, views, height=CONTENT_HEIGHT, width=CONTENT_WIDTH):
        super().__init__(rect(0.0, 0.0, width, height))
        self.views = views
        self.layouts = 0

    def subviews(self):
        return self.views

    def layoutSubtreeIfNeeded(self):
        self.layouts += 1


class Window(View):
    def __init__(self, content, fail_min=False, fail_max=False):
        super().__init__(rect(100.0, 200.0, content.frame().size.width,
                              content.frame().size.height))
        self.content = content
        self.fail_min = fail_min
        self.fail_max = fail_max
        self.constraints = []

    def contentView(self):
        return self.content

    def setContentMinSize_(self, size):
        if self.fail_min:
            raise RuntimeError("minimum size locked")
        self.constraints.append(("min", size))

    def setContentMaxSize_(self, size):
        if self.fail_max:
            raise RuntimeError("maximum size locked")
        self.constraints.append(("max", size))

    def setContentSize_(self, size):
        delta = size.height - self.content.frame().size.height
        self.content.geometry.size = size
        self.geometry.size = types.SimpleNamespace(width=size.width, height=size.height)
        # AppKit keeps subviews attached to the top when the content grows.
        for view in self.content.subviews():
            view.geometry.origin.y += delta

    def setFrameOrigin_(self, point):
        self.geometry.origin = point


FAKE_APPKIT = types.SimpleNamespace(
    NSImageView=ImageView,
    NSTextField=TextField,
    NSTextAlignmentCenter=CENTER_ALIGNMENT,
    NSParagraphStyleAttributeName="NSParagraphStyle",
    NSMutableAttributedString=AttrStringFactory,
    NSMutableParagraphStyle=ParaStyleFactory,
    NSMakeRect=rect,
    NSMakeSize=lambda width, height: types.SimpleNamespace(width=width, height=height),
    NSMakePoint=lambda x, y: types.SimpleNamespace(x=x, y=y))


class NativeLayoutGeometry(SFCase):
    def setUp(self):
        super().setUp()
        self.track_patch(mock.patch.object(alerts, "AppKit", FAKE_APPKIT))
        self.faults = mock.Mock()
        self.track_patch(mock.patch.object(alerts, "_swallow", self.faults))

    def layout(self, wide=True, body_height=90.0, content_height=CONTENT_HEIGHT,
               body_cell=True):
        icon = ImageView(rect(20.0, 242.0, ICON_SIZE, ICON_SIZE))
        title_y = 266.0 if wide else 208.0
        title = TextField(rect(92.0, title_y, 408.0, 18.0), "heading", 22.0)
        body = TextField(rect(92.0, 126.0, 408.0, 90.0), "body", body_height, body_cell)
        button = View(rect(20.0, BUTTON_Y, 480.0, BUTTON_HEIGHT))
        blank = TextField(rect(92.0, 230.0, 408.0, 0.0), " ")
        content = Content([body, icon, title, blank, button], content_height)
        window = Window(content)
        alert = types.SimpleNamespace(window=lambda: window)
        return alert, window, icon, title, body, button, blank

    def assert_centered(self, view, content_width):
        frame = view.frame()
        self.assertAlmostEqual(frame.origin.x + frame.size.width / 2,
                               content_width / 2)

    def assert_stack(self, icon, title, body, button):
        self.assertGreaterEqual(icon.frame().origin.y,
                                title.frame().origin.y + title.frame().size.height)
        self.assertGreaterEqual(title.frame().origin.y,
                                body.frame().origin.y + body.frame().size.height)
        self.assertGreaterEqual(body.frame().origin.y,
                                button.frame().origin.y + button.frame().size.height)

    def test_narrow_layout_preserves_vertical_positions_and_centers_text_and_icon(self):
        alert, window, icon, title, body, button, blank = self.layout(wide=False)
        before = [v.frame().origin.y for v in (icon, title, body, button)]
        center_content(alert)
        self.assertEqual([v.frame().origin.y for v in (icon, title, body, button)], before)
        for view in (icon, title, body):
            self.assert_centered(view, CONTENT_WIDTH)
        self.assertEqual(title.cell_alignment, FAKE_APPKIT.NSTextAlignmentCenter)
        self.assertEqual(body.alignment, FAKE_APPKIT.NSTextAlignmentCenter)
        self.assertEqual(blank.writes, [])
        self.assertEqual(window.constraints, [])
        self.faults.assert_not_called()

    def test_paragraph_style_is_left_alone_by_default(self):
        """默认不碰段落样式：更新说明的编号列表要贴左读（用户 2026-10-23 要求）。
        文案框仍然居中、控件与 cell 仍设居中，只是不改框内每行的对齐。"""
        for wide in (False, True):
            with self.subTest(wide=wide):
                alert, _window, icon, title, body, _button, _blank = self.layout(wide=wide)
                center_content(alert)
                for view in (title, body):
                    self.assertIsNone(view.attributed_written, "默认不该改段落样式")
                    self.assertEqual(view.alignment, CENTER_ALIGNMENT)
                self.assert_centered(body, CONTENT_WIDTH)
                self.assert_centered(icon, CONTENT_WIDTH)
                self.faults.assert_not_called()

    def test_paragraph_style_is_centered_in_both_layouts(self):
        """控件与 cell 居中还不够：文案自带的段落样式写着自然对齐，画字时压过前两者。"""
        for wide in (False, True):
            with self.subTest(wide=wide):
                alert, _window, _icon, title, body, _button, blank = self.layout(wide=wide)
                center_content(alert, True)
                for view in (title, body):
                    self.assertIsNotNone(view.attributed_written, "段落样式没有写回去")
                    self.assertEqual(view.attributed_written.style.alignment(), CENTER_ALIGNMENT)
                self.assertIsNone(blank.attributed_written, "空的副文案占位不该被碰")
                self.faults.assert_not_called()

    def test_paragraph_style_keeps_the_other_attributes(self):
        """只改对齐：原有的换行方式要保留（长编号靠它折行，改成别的会变成截断）。"""
        alert, _window, _icon, title, body, _button, _blank = self.layout(wide=False)
        body.paragraph = ParaStyle(NATURAL_ALIGNMENT, TRUNCATE_TAIL)
        center_content(alert, True)
        self.assertEqual(body.attributed_written.style.lineBreakMode(), TRUNCATE_TAIL)
        self.assertEqual(body.attributed_written.style.alignment(), CENTER_ALIGNMENT)
        self.assertEqual(body.attributed_written.text, body.text, "文字内容不能被改动")

    def test_text_without_a_paragraph_style_still_gets_a_centered_one(self):
        alert, _window, _icon, title, body, _button, _blank = self.layout(wide=False)
        body.paragraph = None
        center_content(alert, True)
        self.assertEqual(body.attributed_written.style.alignment(), CENTER_ALIGNMENT)
        self.faults.assert_not_called()

    def test_failing_to_read_the_attributed_text_is_logged_and_keeps_control_centering(self):
        """取不到带属性字符串：留一条痕迹，控件与 cell 上的居中仍然有效，版式其余部分照常。"""
        alert, _window, icon, title, body, button, _blank = self.layout(wide=False)
        body.attributed_error = RuntimeError("attributed text unavailable")
        center_content(alert, True)
        self.assertEqual(self.faults.call_count, 1, "只有出错的那一段留痕")
        self.assertIn("_center_paragraph", self.faults.call_args.args[0])
        self.assertEqual(body.alignment, CENTER_ALIGNMENT)
        self.assertEqual(body.cell_alignment, CENTER_ALIGNMENT)
        self.assertIsNone(body.attributed_written)
        self.assertEqual(title.attributed_written.style.alignment(), CENTER_ALIGNMENT, "另一段不受牵连")
        self.assert_centered(icon, CONTENT_WIDTH)

    def test_empty_attributed_text_is_left_alone(self):
        view = TextField(rect(20.0, 100.0, 220.0, 20.0), "")
        alerts.NativeAlert._center_paragraph(view)  # discipline-ok(R5): 段落样式修正没有对外入口，直接测它
        self.assertIsNone(view.attributed_written)
        self.faults.assert_not_called()

    def test_wide_layout_reflows_and_restores_button_to_bottom_after_growing(self):
        alert, window, icon, title, body, button, blank = self.layout(body_height=160.0)
        top = window.frame().origin.y + window.frame().size.height
        center_content(alert)
        self.assertGreater(window.frame().size.height, CONTENT_HEIGHT)
        self.assertAlmostEqual(window.frame().origin.y + window.frame().size.height, top)
        self.assertEqual(button.frame().origin.y, BUTTON_Y)
        self.assert_stack(icon, title, body, button)
        for view in (icon, title, body):
            self.assert_centered(view, CONTENT_WIDTH)
        self.assertEqual(body.measurements[0].size.width, CONTENT_WIDTH - 2 * SIDE_MARGIN)
        self.assertEqual(body.frame().size.height, LONG_BODY_MEASURE + TEXT_CLEARANCE)
        self.assertEqual(blank.writes, [])
        self.assertEqual([name for name, _size in window.constraints], ["min", "max"])
        self.faults.assert_not_called()

    def test_wide_layout_with_room_does_not_resize_window(self):
        alert, window, icon, title, body, button, _blank = self.layout(content_height=600.0)
        center_content(alert)
        self.assertEqual(window.constraints, [])
        self.assert_stack(icon, title, body, button)
        self.faults.assert_not_called()

    def test_extreme_body_is_clipped_above_controls_and_window_growth_is_bounded(self):
        alert, window, icon, title, body, button, _blank = self.layout(body_height=10000.0)
        center_content(alert)
        self.assertLessEqual(window.frame().size.height, CONTENT_HEIGHT + 480.0)
        self.assert_stack(icon, title, body, button)
        self.assertGreater(body.frame().size.height, EMPTY_HEIGHT)
        self.assertLess(body.frame().size.height, HUGE_TEXT_HEIGHT)
        self.faults.assert_not_called()

    def test_empty_text_is_not_mistaken_for_bottom_control(self):
        alert, _window, icon, title, body, button, blank = self.layout()
        blank.geometry = rect(0, 290, CONTENT_WIDTH, 10)
        center_content(alert)
        self.assert_stack(icon, title, body, button)
        self.assertEqual(blank.writes, [])
        self.faults.assert_not_called()

    def test_hidden_bottom_control_does_not_raise_the_floor(self):
        # 无按钮提示（如「反馈上传中」）把按钮区藏起来：被藏起来的控件不能再算「底部控件带」，
        # 否则文案会被往上顶、窗口白白长高。同样的内容，藏起来后的窗口必须更矮。
        shown_alert, shown_window, *_ = self.layout(body_height=160.0)
        center_content(shown_alert)
        alert, window, _icon, _title, _body, button, _blank = self.layout(body_height=160.0)
        button.setHidden_(True)
        center_content(alert)
        self.assertLess(window.frame().size.height, shown_window.frame().size.height)
        self.faults.assert_not_called()

    def hide_layout(self, **kwargs):
        alert, window, icon, title, body, stack, _blank = self.layout(**kwargs)
        ok = Button(rect(0.0, 0.0, 60.0, 28.0))
        alert.buttons = lambda: [ok]
        return alert, window, icon, title, body, stack, ok

    def test_hiding_the_button_band_disables_buttons_and_closes_the_gap(self):
        # 「反馈上传中」不要按钮：NSAlert 自己会补一个 OK，只能事后藏起来并把窗口收矮。
        alert, window, _icon, _title, body, stack, ok = self.hide_layout(wide=False)
        hide_button_band(alert)
        self.assertTrue(stack.isHidden())
        self.assertTrue(ok.isHidden())
        self.assertFalse(ok.enabled, "被藏起来的按钮不能再响应点击")
        self.assertEqual(ok.key, "", "被藏起来的按钮不能再响应回车/Esc")
        self.assertEqual(body.frame().origin.y, BOTTOM_GAP,
                         "最低的可见文案应落在正常的底部留白上，而不是留一条空白带")
        self.assertEqual(window.frame().size.height, CONTENT_HEIGHT - (BODY_Y - BOTTOM_GAP))
        self.faults.assert_not_called()

    def test_hiding_the_band_with_nothing_to_anchor_leaves_the_window_alone(self):
        stack = View(rect(20.0, BUTTON_Y, 480.0, BUTTON_HEIGHT))
        window = Window(Content([stack]))
        ok = Button(rect(0.0, 0.0, 60.0, 28.0))
        hide_button_band(types.SimpleNamespace(window=lambda: window, buttons=lambda: [ok]))
        self.assertTrue(stack.isHidden())
        self.assertEqual(window.constraints, [])
        self.assertEqual(window.frame().size.height, CONTENT_HEIGHT)
        self.faults.assert_not_called()

    def test_hiding_the_band_keeps_a_window_that_has_no_room_to_shrink(self):
        alert, window, _icon, _title, body, _stack, _ok = self.hide_layout(wide=False)
        body.geometry = rect(92.0, BOTTOM_GAP - 5.0, 408.0, BODY_HEIGHT)
        hide_button_band(alert)
        self.assertEqual(window.constraints, [])
        self.assertEqual(window.frame().size.height, CONTENT_HEIGHT)
        self.faults.assert_not_called()

    def test_hiding_the_band_failure_is_logged_not_raised(self):
        def window_gone():
            raise RuntimeError("window gone")

        hide_button_band(types.SimpleNamespace(window=window_gone, buttons=lambda: []))
        self.assertEqual(self.faults.call_count, 1)

    def test_missing_icon_or_missing_text_leaves_system_layout_untouched(self):
        for views in ([TextField(rect(10, 20, 80, 20))],
                      [ImageView(rect(10, 20, ICON_SIZE, ICON_SIZE))]):
            with self.subTest(views=type(views[0]).__name__):
                window = Window(Content(views))
                center_content(types.SimpleNamespace(window=lambda: window))
                self.assertEqual(views[0].writes, [])
        self.faults.assert_not_called()

    def test_no_bottom_control_still_reflows(self):
        alert, window, icon, title, body, button, _blank = self.layout()
        window.content.views.remove(button)
        center_content(alert)
        self.assertGreaterEqual(body.frame().origin.y, SIDE_MARGIN)
        self.assert_centered(icon, CONTENT_WIDTH)
        self.assertGreaterEqual(title.frame().origin.y, body.frame().origin.y)
        self.faults.assert_not_called()

    def test_missing_cell_preserves_frame_height_and_logs_measurement_failure(self):
        alert, _window, icon, title, body, button, _blank = self.layout(body_cell=False)
        center_content(alert)
        self.assertEqual(body.frame().size.height, BODY_HEIGHT)
        self.assert_stack(icon, title, body, button)
        self.assertEqual(self.faults.call_count, 1)
        self.assertIn("_text_height", self.faults.call_args.args[0])
        self.assertEqual(body.alignment, FAKE_APPKIT.NSTextAlignmentCenter)

    def test_layout_failure_leaves_a_named_trace(self):
        alert = types.SimpleNamespace(window=mock.Mock(side_effect=RuntimeError("window lost")))
        center_content(alert)
        self.faults.assert_called_once()
        self.assertIn("_center_content", self.faults.call_args.args[0])

    def test_positive_cell_height_adds_small_clearance(self):
        text = TextField(rect(0, 0, 40, 12), needed_height=25)
        self.assertEqual(text_height(text, 200), MEASUREMENT_CELL_HEIGHT + TEXT_CLEARANCE)
        self.assertEqual(text.measurements[0].size.width, MEASUREMENT_BOX_WIDTH)
        self.faults.assert_not_called()

    def test_zero_or_negative_measurement_falls_back_to_original_height(self):
        for measured in (0, -1):
            text = TextField(rect(0, 0, 40, 12), needed_height=measured)
            self.assertEqual(text_height(text, 200), MEASUREMENT_FRAME_HEIGHT)
        self.faults.assert_not_called()

    def test_measurement_exception_falls_back_and_is_named(self):
        text = TextField(rect(0, 0, 40, 12), needed_height=RuntimeError("no cell metrics"))
        self.assertEqual(text_height(text, 200), MEASUREMENT_FRAME_HEIGHT)
        self.assertIn("_text_height", self.faults.call_args.args[0])

    def test_one_broken_size_constraint_does_not_prevent_window_growth(self):
        for constraint in ("fail_min", "fail_max"):
            with self.subTest(constraint=constraint):
                self.faults.reset_mock()
                window = Window(Content([]), **{constraint: True})
                top = window.frame().origin.y + window.frame().size.height
                grow_window(window, CONTENT_WIDTH, 600)
                self.assertEqual(window.frame().size.height, ROOMY_HEIGHT)
                self.assertEqual(window.frame().origin.y + window.frame().size.height, top)
                self.assertEqual(len(window.constraints), 1)
                self.faults.assert_called_once()
                self.assertIn("_grow_window", self.faults.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
