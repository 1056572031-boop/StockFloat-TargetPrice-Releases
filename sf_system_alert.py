# -*- coding: utf-8 -*-
"""macOS 原生弹窗桥接：首选 NSAlert（真·系统弹窗），降级 osascript。

为什么不是 QMessageBox：它是 Qt 自绘窗口（气泡图标、红绿灯按钮），用户已两次
反馈"丑"（v1.4.5）。
为什么首选 NSAlert：macOS"系统默认弹窗"的本体 —— 无标题栏、粗体主文案、
按钮竖排占满底部、默认按钮蓝色居上，与系统设置/软件更新等对话框完全一致。
v1.4.6 曾用 osascript display dialog：样式接近但仍是"对话框"版式（带标题栏、
按钮横排右下），用户反馈未与原生统一（v1.4.8 换 NSAlert）。

不用 NSAlert.runModal：模态循环与退出路径叠加曾触发 SIGSEGV（v1.4.3 修复记录）。
这里用非模态展示（orderFront + 手动接管按钮 action），点击回调发生在 Qt 主线程
的 AppKit 事件里；退出收口只需 orderOut，本进程无 Qt 窗口析构竞态。
AppKit 不可用/非主线程时自动降级 osascript 子进程方案（v1.4.6 实现保留）。

落点（v1.4.38）：NSAlert 路径一律摆到**悬浮窗所在屏幕可用区的正中**，见本节下方
「弹窗落点」一段与 target_screen()/center_window()。**osascript 降级路径做不到这一
点** —— AppleScript 的 display dialog 没有位置参数，事后用 System Events 挪窗口要
辅助功能权限、会额外弹一次授权框，比位置偏更糟。该降级在装机版里走不到（冻结产物
在主线程、AppKit 可用），所以保持原样，只在这里写清边界。
"""

import os
import subprocess
import sys
import threading

try:
    import AppKit
    import objc
except Exception:                                    # 非 macOS / 依赖缺失
    AppKit = None
    objc = None


# 降级路径：固定 AppleScript 源码 + argv 参数化传文本。
# v1.4.8 安全扫描（L3 中危 CWE-78）整改：外部来源文本（OTA 清单的版本号/
# 更新说明）**绝不插值进脚本源码**，全部作为 osascript 运行参数传入，由
# `on run argv` 读取 —— 文本里出现引号/反斜杠/换行/AppleScript 关键字都
# 只是普通字符，不存在脚本注入面。
FALLBACK_SCRIPT = '''on run argv
	set theTitle to item 1 of argv
	set theText to item 2 of argv
	set theDefault to item 3 of argv
	set theCancel to item 4 of argv
	set theIcon to item 5 of argv
	set n to (item 6 of argv) as integer
	set btns to {}
	repeat with i from 7 to (6 + n)
		set end of btns to (item i of argv)
	end repeat
	activate
	if theIcon is "" then
		if theCancel is "" then
			return display dialog theText buttons btns default button theDefault with title theTitle
		else
			return display dialog theText buttons btns default button theDefault cancel button theCancel with title theTitle
		end if
	else
		if theCancel is "" then
			return display dialog theText buttons btns default button theDefault with title theTitle with icon (POSIX file theIcon)
		else
			return display dialog theText buttons btns default button theDefault cancel button theCancel with title theTitle with icon (POSIX file theIcon)
		end if
	end if
end run'''


def build_launch(title, text, buttons=(), default=None, cancel=None,
                 icon_path=None):
    """拼 osascript 启动参数：[固定脚本, 各动态文本…]；脚本源码保持常量。"""
    titles = list(buttons) or ["好"]
    if not default:
        default = titles[-1]
    return [FALLBACK_SCRIPT,
            title or "提示",
            str(text),
            default,
            cancel or "",
            icon_path or "",
            str(len(titles)),
            *titles]


def parse_button(output):
    """从 osascript 的 'button returned:X, gave up:false' 中取按钮名。"""
    for part in (output or "").replace("\n", ",").split(","):
        part = part.strip()
        if part.startswith("button returned:"):
            return part.split(":", 1)[1].strip()
    return None


def _swallow(where, exc):
    """给"被吞掉的异常"留痕（AGENTS.md §6 第 9 条：热路径兜异常不许静默）。

    本模块**刻意不在顶层** import sf_logging：它会拉 PyQt6，而这里的 osascript
    降级路径要在任何环境里都 import 得动。留痕失败时无路可记（记日志的日志不能
    自我递归），所以这一处保持静默。
    """
    try:
        from sf_logging import log_swallow  # noqa: F401
        log_swallow(where, exc)
    except Exception:                                        # noqa: BLE001
        pass


def bundled_icon_path():
    """定位包内 icon.icns（弹窗左侧显示应用图标）；找不到返回 None。"""
    candidates = []
    base = getattr(sys, "_MEIPASS", None)
    if base:
        candidates.append(os.path.join(base, "icon.icns"))
        candidates.append(os.path.abspath(os.path.join(
            os.path.dirname(getattr(sys, "executable", "")),
            os.pardir, "Resources", "icon.icns")))
    candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "icon.icns"))
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


# ---------------------------------------------------------------------------
# 弹窗落点：屏幕正中（用户 2026-10-08 要求：检查更新相关的窗口一律居中）
#
# 实测依据（本机 1470x956 单屏，真弹出来量）：NSAlert 自己**不居中** ——
# 「正在检查更新」落在 (221,581) 260x176，窗口中心比屏幕可用区中心
# 偏左 384pt、偏上 178pt；长更新说明那版偏左 254pt、偏上 70pt。
# 也就是说此前每一次检查更新，弹窗都贴在屏幕左上角附近，副屏时更离谱。
# ---------------------------------------------------------------------------

# 悬浮窗所在屏幕的中心点，**Qt 全局坐标**（原点 = 菜单栏屏左上角，y 向下）。
# 由产品侧在弹窗前调 set_screen_anchor() 写入。这里只存两个数字、**不在顶层
# import Qt**：osascript 降级路径要在任何环境里都 import 得动（见 _swallow）。
_screen_anchor = None


def set_screen_anchor(x, y):
    """登记『该弹在哪块屏』：传悬浮窗所在屏中心的 Qt 全局坐标。

    为什么需要登记：本应用是悬浮窗程序，弹窗没有属主窗口可跟。不登记就只能跟
    主屏/鼠标走，副屏上用『检查更新』时弹窗会跑到另一块屏正中，用户看不到
    （与 v1.2.71 修替换股票编辑器跳屏是同一个病根）。取值非法时清空登记，
    由 target_screen() 退回鼠标所在屏。
    """
    global _screen_anchor
    try:
        _screen_anchor = (float(x), float(y))
    except (TypeError, ValueError):
        _screen_anchor = None


def screen_anchor():
    """当前登记的选屏锚点（Qt 全局坐标），未登记/取值非法时为 None。

    存在的理由：产品侧「有没有把悬浮窗所在屏告诉弹窗模块」这件事需要一个公开
    出口可查，否则用例只能去读模块私有变量（AGENTS.md §6 第 5 条）。
    """
    return _screen_anchor


def _qt_to_ns(x, y):
    """Qt 全局坐标 → NS 全局坐标。

    两套坐标只差在 y 轴方向与原点：Qt 原点在菜单栏屏**左上角**、y 向下；
    NS 原点在同一块屏的**左下角**、y 向上。菜单栏屏就是 NS 里 origin==(0,0)
    的那块，它的高度即翻转基准。
    """
    screens = list(AppKit.NSScreen.screens())
    if not screens:
        return None
    height = 0.0
    for screen in screens:
        frame = screen.frame()
        if abs(frame.origin.x) < 1.0 and abs(frame.origin.y) < 1.0:
            height = float(frame.size.height)
            break
    if height <= 0.0:
        height = float(screens[0].frame().size.height)
    return (float(x), height - float(y))


def _screen_containing(point):
    """按 NS 全局坐标找屏；点不在任何屏内（脏配置、刚拔掉副屏）返回 None。"""
    if point is None:
        return None
    x, y = point
    for screen in AppKit.NSScreen.screens():
        frame = screen.frame()
        if (frame.origin.x <= x < frame.origin.x + frame.size.width
                and frame.origin.y <= y < frame.origin.y + frame.size.height):
            return screen
    return None


def target_screen():
    """这块弹窗该落在哪块屏：悬浮窗所在屏 → 鼠标所在屏 → 主屏 → 第一块屏。"""
    if AppKit is None:
        return None
    if _screen_anchor is not None:
        screen = _screen_containing(_qt_to_ns(*_screen_anchor))
        if screen is not None:
            return screen
    try:
        mouse = AppKit.NSEvent.mouseLocation()
        screen = _screen_containing((float(mouse.x), float(mouse.y)))
        if screen is not None:
            return screen
    except Exception as exc:                             # noqa: BLE001
        _swallow("sf_system_alert.target_screen(退回主屏)", exc)
    try:
        main = AppKit.NSScreen.mainScreen()
        if main is not None:
            return main
        screens = list(AppKit.NSScreen.screens())
        return screens[0] if screens else None
    except Exception as exc:                             # noqa: BLE001
        _swallow("sf_system_alert.target_screen(取屏失败)", exc)
        return None


def centered_origin(visible, win_size):
    """纯几何：窗口在『屏可用区』正中的左下原点（NS 坐标，y 向上）。

    visible = (x, y, w, h) 已扣掉菜单栏与 Dock 的可用区；win_size = (w, h)。
    用可用区而不是整块屏：这样视觉上就是正中，且永远不会被菜单栏或 Dock 压住。
    窗口比可用区还大时（超长更新说明）钳在可用区内，优先保住底部按钮可点。
    单独成函数是为了能在没有窗口服务的机器上跑用例。
    """
    vx, vy, vw, vh = (float(v) for v in visible)
    ww, wh = (float(v) for v in win_size)
    x = vx + (vw - ww) / 2.0
    y = vy + (vh - wh) / 2.0
    x = min(max(x, vx), vx + max(0.0, vw - ww))
    y = min(max(y, vy), vy + max(0.0, vh - wh))
    return (round(x), round(y))


def center_window(window):
    """把窗口摆到目标屏可用区正中；量不到屏就什么都不做。

    失败顺序上宁可『弹出来但没居中』也不能『弹不出来』，所以这里全程兜异常，
    但必须留痕（AGENTS §6 第 9 条）：静默兜住就是几十个版本看不见位置偏。
    """
    try:
        screen = target_screen()
        if screen is None:
            return False
        frame = window.frame()
        visible = screen.visibleFrame()
        x, y = centered_origin(
            (visible.origin.x, visible.origin.y,
             visible.size.width, visible.size.height),
            (frame.size.width, frame.size.height))
        window.setFrameOrigin_(AppKit.NSMakePoint(x, y))
        return True
    except Exception as exc:                             # noqa: BLE001
        _swallow("sf_system_alert.center_window(弹窗未居中)", exc)
        return False


class SystemDialog:
    """osascript 降级路径句柄：后台线程等待点击，close() 主动收口。

    launch 为 build_launch() 的产物（[常量脚本, 动态参数…]），动态文本
    只出现在参数位，绝不进入脚本源码。
    """

    def __init__(self, launch, on_result=None):
        self._launch = list(launch)
        self._on_result = on_result
        self._proc = None
        self._closing = False
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="system-dialog")
        self._thread.start()

    def _run(self):
        button = None
        if not self._closing:
            try:
                self._proc = subprocess.Popen(
                    ["/usr/bin/osascript", "-e"] + self._launch,
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    text=True)
                button = parse_button(self._proc.communicate()[0])
            except OSError:
                button = None
            finally:
                self._proc = None
        if self._closing:
            button = None
        if button is not None and self._on_result is not None:
            try:
                self._on_result(button)
            except Exception:
                pass

    def close(self):
        """收口：terminate 弹窗进程；重复调用安全。"""
        self._closing = True
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass


if AppKit is not None:

    class _AlertTarget(AppKit.NSObject):
        """把 NSAlert 按钮 action 交还 Python 句柄（非模态接管）。"""

        def setOwner_(self, owner):
            """把宿主 Python 对象交给这个 ObjC 侧 target（弹窗建好后调一次）。

            名字末尾的下划线是 **ObjC selector**（`setOwner:`），不是 Python 私有约定，
            按 AGENTS §7 不动；它存的是 Python 对象引用，只做回调转发，不参与任何判定。
            """
            self._owner = owner

        def buttonClicked_(self, sender):
            """AppKit 的按钮回调（selector `buttonClicked:`，由 setTarget_/setAction_ 绑上）。

            交回的是**按钮文案**而不是索引：本模块的语义全押在文案上（"立即更新"/"稍后"…，
            addButtonWithTitle_ 逐个建），所以 `_handle_click(title)` 按文案分派。
            兜异常但**必须留痕**（AGENTS §6 第 9 条）：这里一抛错，用户看到的就是
            "点了按钮什么也没发生"——弹窗不关、下载不开始，界面上一丝线索都没有。
            2026-10-13 前这一层是纯 `pass`，而 `_handle_click` 只给"结果回调"那一段留了痕，
            更早的 `_detach()` 抛错照样蒸发；现在整段接 `_swallow`。
            """
            try:
                owner = getattr(self, "_owner", None)
                if owner is not None:
                    owner._handle_click(str(sender.title()))
            except Exception as exc:
                _swallow("sf_system_alert.buttonClicked_(点了按钮但没任何反应)", exc)


else:
    _AlertTarget = None


class NativeAlert:
    """NSAlert 非模态句柄：外观与系统弹窗完全一致，点击回调在主线程。

    buttons 顺序约定沿用调用方（如 ("暂不更新","立即更新")）；NSAlert 的
    default 按钮必须**第一个**添加（蓝色、位置最右/最上），cancel 最后添加并
    绑定 Esc。close() 后结果被丢弃（程序收口，不是用户选择）。

    progress=True 时在正文下方挂一条系统进度条（NSProgressIndicator，确定型），
    用 set_progress() 刷新 —— 这是 v1.4.17 给"下载更新"用的：osascript 与
    NSAlert 的纯文案版式都装不下进度条，此前只能退回 Qt 自绘框。
    """

    _live = {}          # 防 GC：AppKit target/action 链需要 Python 侧存活引用

    def __init__(self, message, info, buttons, default, cancel,
                 icon_path, on_result, progress=False):
        self._on_result = on_result
        self._closed = False
        self._bar = None
        self._last_shown = None
        self._space_timer = None
        self._space_ticks = 0
        alert = AppKit.NSAlert.alloc().init()
        alert.setMessageText_(message)
        if info:
            alert.setInformativeText_(info)
        if icon_path and os.path.isfile(icon_path):
            image = AppKit.NSImage.alloc().initWithContentsOfFile_(icon_path)
            if image is not None:
                alert.setIcon_(image)
        if progress:
            self._bar = self._make_bar(alert)
        titles = list(buttons)
        if default and default in titles:
            titles.remove(default)
            titles.insert(0, default)
        if cancel and cancel in titles:
            titles.remove(cancel)
            titles.append(cancel)
        for title in titles:
            alert.addButtonWithTitle_(title)
        if cancel and titles:
            alert.buttons()[-1].setKeyEquivalent_("\x1b")   # Esc = 取消
        for button in alert.buttons():
            # 焦点描边：按钮成为第一响应者会被一圈蓝色描边套住（用户要求去掉）。
            button.setFocusRingType_(AppKit.NSFocusRingTypeNone)
        alert.layout()
        self._alert = alert
        self._window = alert.window()
        self._window.setReleasedWhenClosed_(False)
        self._center_content(alert)
        self._target = _AlertTarget.alloc().init()
        self._target.setOwner_(self)
        for button in alert.buttons():
            button.setTarget_(self._target)
            button.setAction_("buttonClicked:")
        # v1.4.45 复核 B3：先摆到正中，再上屏 —— 首帧就在该在的位置，
        # 用户看不见"先跳出来再挪过去"那一下（实测依据见本方法末尾的注释）。
        self.present()
        # 落点：屏幕正中（v1.4.38）。v1.4.45 复核 B3 改成**上屏前先摆一次**：
        # 原注释说"提前挪会被 AppKit 覆盖"，本机 2026-10-08 用真 NSAlert 实测
        # （layout 之后摆位 → makeKeyAndOrderFront → 读回原点）：
        #   目标 (605,390) → 摆位后 (605,390) → 上屏后 (605,390)，没有被覆盖。
        # 所以先摆再上屏，用户就不会看见"先出现在旧位、再跳正中"那一下；
        # 上屏后再调一次是幂等的 —— 位置没变就等于没动，仍在真被系统重排时兜住。
        center_window(self._window)
        NativeAlert._live[id(self)] = self

    def present(self):
        """Bring an existing alert to the current Space without creating another one."""
        if self._closed:
            return False
        from sf_glass import (TOOL_WINDOW_LEVEL, POPUP_SPACE_TICK_MS,
                              popup_join_all_spaces_behavior)
        window = self._window
        behavior = popup_join_all_spaces_behavior(window.collectionBehavior())
        behavior |= AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary
        window.setCollectionBehavior_(behavior)
        window.setLevel_(TOOL_WINDOW_LEVEL)
        window.setHidesOnDeactivate_(False)
        center_window(window)
        window.makeKeyAndOrderFront_(None)
        if AppKit.NSApp is not None:
            AppKit.NSApp.activateIgnoringOtherApps_(True)
        self._space_ticks = 0
        if not self._join_active_space():
            from PyQt6.QtCore import QCoreApplication, QTimer
            if QCoreApplication.instance() is not None:
                if self._space_timer is None:
                    self._space_timer = QTimer()
                    self._space_timer.setInterval(POPUP_SPACE_TICK_MS)
                    self._space_timer.timeout.connect(self._retry_active_space)
                self._space_timer.start()
        return True

    def _join_active_space(self):
        if self._closed:
            return True
        try:
            if self._window.isOnActiveSpace():
                return True
            from sf_glass import cgs_add_window_to_space
            cgs_add_window_to_space(int(self._window.windowNumber()))
            self._window.orderFrontRegardless()
            return bool(self._window.isOnActiveSpace())
        except Exception as exc:
            _swallow("sf_system_alert.NativeAlert.join_active_space", exc)
            return False

    def _retry_active_space(self):
        from sf_glass import POPUP_SPACE_MAX_TICKS
        timer = self._space_timer
        if timer is None:
            return
        self._space_ticks += 1
        if self._closed or self._join_active_space():
            timer.stop()
        elif self._space_ticks >= POPUP_SPACE_MAX_TICKS:
            timer.stop()
            _swallow("sf_system_alert.NativeAlert.active_space_timeout",
                     RuntimeError("alert did not join the active Space"))

    # 内容版式的度量（AppKit 自己的默认值，集中一处便于按肉眼调）
    _TOP_PAD = 20.0        # 图标上边距
    _ICON_GAP = 12.0       # 图标底 → 主文案顶
    _TEXT_GAP = 14.0       # 相邻两段文案
    _SIDE_MARGIN = 20.0    # 文案左右留白（图标让出横向空间后可以拉满）
    _BOTTOM_GAP = 20.0     # 末段文案底 → 下方控件（进度条 / 按钮区）
    _MAX_GROW = 480.0      # 窗口最多长高多少，异常内容下不失控

    @staticmethod
    def _center_content(alert):
        """把 NSAlert 内容改成「图标在上、整体水平居中」，且互不重叠。

        为什么必须自己排（v1.4.22 的初衷）：系统默认把图标放在**文案左侧**，
        用户要求"图标在上、居中上下展示"。

        为什么 v1.4.22 的实现会重叠（本轮修，老板 2026-09-24 截图）：它只把图标
        **水平**挪到中线、y 一动不动。而 NSAlert 其实有两套版式：
          · 窄版式（文案短）：图标本来就在文案上方 → 看起来正常，旧用例恰好只有它；
          · 宽版式（文案长）：图标与主文案**同一条横带**，实测 macOS 27
            图标 y=242.5~306.5 / x=20~84，主文案 y=266.5~282.5 / x=92~500。
        更新说明属于宽版式，图标被挪到水平中线时正好压住标题 —— 旧用例只量了
        "水平中心偏移为 0"，从没量过纵向，所以绿着发了十几个版本。

        现在按版式分两条路：
          · 已上下排好（窄版式）→ 只补水平居中，纵向一律不动；
          · 图标与文案同带（宽版式）→ 按「图标 → 主文案 → 副文案 →按钮」自上
            而下重排。两处配套缺一不可：
              ① 文案框拉满宽度（图标不再占横向空间）并按新宽度**重算需要多高**，
                 沿用系统给的旧高度会裁掉最后几行；
              ② 纵向排不下就把弹窗长高，并把按钮钉回贴底位置（AppKit 的子视图
                 跟着顶边走），而不是让正文盖到按钮上、或在底部留一条空白带。

        失败顺序也有讲究：先让文案离开图标横带，最后才挪图标 —— 中途出错最坏
        退成"图标在左"的系统版式，绝不会退成"图标压在字上"。弹窗**弹出来但没
        居中**远好于**弹不出来**；但失败必须留痕（log_swallow），否则又会像这次
        一样静默几十版。
        """
        try:
            window = alert.window()
            content = window.contentView()
            content.layoutSubtreeIfNeeded()
            width = content.frame().size.width
            height = content.frame().size.height

            icon = None
            texts = []
            others = []
            for view in content.subviews():
                if icon is None and isinstance(view, AppKit.NSImageView):
                    icon = view
                elif isinstance(view, AppKit.NSTextField):
                    # 空的是副文案占位（无副文案时系统仍会留一个），画不出东西，
                    # 也不能算进底部控件带 —— 算进去会让 floor 顶到文案区里。
                    if str(view.stringValue() or "").strip():
                        texts.append(view)
                else:
                    others.append(view)
            if icon is None or not texts:
                return

            def top_of(view):
                frame = view.frame()
                return frame.origin.y + frame.size.height

            # 底部控件带：按钮，以及下载进度条那类 accessory。内容不得压上去。
            floor = max([top_of(v) for v in others] or [0.0])
            # AppKit 坐标系 y 越大越靠上：按系统给的上下顺序排（标题在正文上面）
            texts.sort(key=lambda v: -v.frame().origin.y)

            box = max(40.0, width - 2 * NativeAlert._SIDE_MARGIN)
            icon_frame = icon.frame()
            first = texts[0].frame()
            if icon_frame.origin.y >= first.origin.y + first.size.height:
                # 短文案走的是"窄版式"：系统本来就把图标排在文案上方，
                # 这里只补水平居中，**一律不动纵向**。
                # 为什么不动：纵向一动就要长高，而长高会把贴底的按钮栈
                # （NSStackView，受约束管）顶起来，底部多出一条空白带。
                for view in texts:
                    frame = view.frame()
                    NativeAlert._place(view, NativeAlert._SIDE_MARGIN,
                                       frame.origin.y, box, frame.size.height)
                    NativeAlert._center_text(view)
                NativeAlert._place(icon, (width - icon_frame.size.width) / 2.0,
                                   icon_frame.origin.y, icon_frame.size.width,
                                   icon_frame.size.height)
                return

            # 长文案走的是"宽版式"：图标与主文案在同一条横带里 —— 必须重排成
            # 上下结构，并把文案按新宽度重算高度（沿用旧高度会裁掉最后几行）。
            heights = [NativeAlert._text_height(v, box) for v in texts]
            icon_h = icon_frame.size.height
            stack = (NativeAlert._TOP_PAD + icon_h + NativeAlert._ICON_GAP
                     + sum(heights)
                     + NativeAlert._TEXT_GAP * len(heights)
                     + NativeAlert._BOTTOM_GAP + floor)
            if stack > height:
                # 纵向放不下：弹窗长高。AppKit 的子视图跟着**顶边**走（实测
                # macOS 27 长高 86pt 后按钮从 y=16 跑到 y=102），所以按长高前
                # 记录的 y 把它们钉回贴底位置，否则底部出现空白带。
                base = [(v, v.frame().origin.y) for v in others]
                height = min(height + NativeAlert._MAX_GROW, stack)
                NativeAlert._grow_window(window, width, height)
                content.layoutSubtreeIfNeeded()
                for view, y0 in base:
                    frame = view.frame()
                    NativeAlert._place(view, frame.origin.x, y0,
                                       frame.size.width, frame.size.height)

            # 自上而下排：图标 → 主文案 → 副文案。先排文案、最后才挪图标：
            # 中途出错最坏退成"图标在左"的系统版式，绝不会退成"图标压在字上"。
            icon_y = height - NativeAlert._TOP_PAD - icon_h
            cursor = icon_y - NativeAlert._ICON_GAP
            limit = floor + NativeAlert._BOTTOM_GAP
            for view, h in zip(texts, heights):
                h = min(h, max(0.0, cursor - limit))
                cursor -= h
                NativeAlert._place(view, NativeAlert._SIDE_MARGIN, cursor, box, h)
                NativeAlert._center_text(view)
                cursor -= NativeAlert._TEXT_GAP
            icon_w = icon.frame().size.width
            NativeAlert._place(icon, (width - icon_w) / 2.0, icon_y,
                               icon_w, icon_h)
        except Exception as exc:                             # noqa: BLE001
            _swallow("sf_system_alert._center_content(弹窗版式退回系统默认)", exc)

    @staticmethod
    def _place(view, x, y, w, h):
        view.setFrame_(AppKit.NSMakeRect(x, y, w, h))

    @staticmethod
    def _center_text(view):
        """文案居中对齐：控件与 cell 两处都设，只设一处时某些系统版本不生效。"""
        view.setAlignment_(AppKit.NSTextAlignmentCenter)
        cell = view.cell()
        if cell is not None:
            cell.setAlignment_(AppKit.NSTextAlignmentCenter)

    @staticmethod
    def _text_height(view, box_width):
        """按拉宽后的文案框问 AppKit 需要多高；问不出来就沿用系统给的高度。

        返回值再垫 2pt：实测 `boundingRect` 与 `cellSizeForBounds_` 会有 0.1pt
        量级的出入，差一点就会把最后一行的下沿削掉。
        """
        try:
            need = view.cell().cellSizeForBounds_(
                AppKit.NSMakeRect(0.0, 0.0, box_width, 1.0e6)).height
            if need > 0:
                return float(need) + 2.0
        except Exception as exc:                             # noqa: BLE001
            _swallow("sf_system_alert._text_height", exc)
        return float(view.frame().size.height)

    @staticmethod
    def _grow_window(window, width, height):
        """弹窗长高，顶边保持不动（内容变多时不让它往下跑）。

        NSAlert 把窗口锁成不可调，必须先放开 content 的 min/max，
        否则 setContentSize_ 会被 AppKit 夹回原尺寸。
        """
        frame = window.frame()
        top = frame.origin.y + frame.size.height
        size = AppKit.NSMakeSize(width, height)
        for setter in (window.setContentMinSize_, window.setContentMaxSize_):
            try:
                setter(size)
            except Exception as exc:                         # noqa: BLE001
                _swallow("sf_system_alert._grow_window(content 尺寸锁)", exc)
        window.setContentSize_(size)
        new_frame = window.frame()
        window.setFrameOrigin_(AppKit.NSMakePoint(
            new_frame.origin.x, top - new_frame.size.height))

    @staticmethod
    def _make_bar(alert):
        """确定型进度条作为 NSAlert 的 accessoryView（系统自带的进度控件）。"""
        bar = AppKit.NSProgressIndicator.alloc().initWithFrame_(
            AppKit.NSMakeRect(0.0, 0.0, 260.0, 20.0))
        style = getattr(AppKit, "NSProgressIndicatorStyleBar", None)
        if style is not None:
            bar.setStyle_(style)
        bar.setIndeterminate_(False)
        bar.setMinValue_(0.0)
        bar.setMaxValue_(100.0)
        bar.setDoubleValue_(0.0)
        alert.setAccessoryView_(bar)
        return bar

    def set_progress(self, pct, text=""):
        """刷新进度：数值给进度条，文字给副文案。

        进度回调来自下载线程转投的主线程队列，条数可能很密；百分比与文案都没变
        时直接返回，避免每条网络分片都让 AppKit 重排一次弹窗。
        """
        if self._closed or self._bar is None:
            return False
        value = max(0, min(100, int(pct)))
        label = text or ("%d%%" % value)
        if self._last_shown == (value, label):
            return False
        self._last_shown = (value, label)
        try:
            self._bar.setDoubleValue_(float(value))
            self._bar.display()
            self._alert.setInformativeText_(label)
            self._alert.layout()
            # 重排会按 NSAlert 自己的版式摆回来，居中必须跟着补一次。
            self._center_content(self._alert)
            # 重排也可能让窗口长高一截（副文案换行）：只补内容居中，窗口会
            # 越刷越往上跑，所以落点也要跟着补一次。
            center_window(self._window)
        except Exception:
            return False
        return True

    def _handle_click(self, title):
        if self._closed:
            return
        self._detach()
        if self._on_result is not None:
            try:
                self._on_result(title)
            except Exception as exc:
                # 必须留痕：这里吞掉的正是「用户点了『立即更新』却什么都没发生」——
                # 弹窗已经关掉，用户唯一的反馈是没有开始下载，静默兜住等于功能消失
                # 且无线索（AGENTS §6 第 9 条）。走本模块的 _swallow：它在任何环境
                # 都 import 得动（顶层 import sf_logging 会拉进 PyQt6）。
                _swallow("sf_system_alert._handle_click", exc)

    def _detach(self):
        self._closed = True
        if self._space_timer is not None:
            self._space_timer.stop()
        NativeAlert._live.pop(id(self), None)
        try:
            self._window.orderOut_(None)
        except Exception:
            pass

    def close(self):
        """程序主动收口；重复调用安全，结果不再回调。"""
        if not self._closed:
            self._detach()


def native_available():
    """NSAlert 路径可用性：AppKit 已导入 且 当前在进程主线程。

    AppKit 窗口必须在主线程创建；非主线程调用（如后台核验脚本）走 osascript
    降级路径，行为与 v1.4.6 一致。
    """
    return AppKit is not None and threading.current_thread() is threading.main_thread()


def show_alert(title, text, buttons=("好",), default=None, cancel=None,
               icon_path=None, on_result=None, message=None, info=None):
    """弹一条 macOS 原生样式对话框，立即返回句柄，不阻塞调用线程。

    message/info 用于 NSAlert 的粗体主文案与副文案；未提供时以 text 兜底
    （osascript 降级路径始终使用合并后的 text）。
    """
    if default is None and buttons:
        default = buttons[-1]
    if native_available():
        try:
            return NativeAlert(message or text, info or "", buttons,
                               default, cancel, icon_path, on_result)
        except Exception as exc:
            # 这里是"降不降级"的决策点，必须留痕（AGENTS §6 第 9 条；复核 B1）：
            # 静默吞掉的后果是弹窗既不居中、也可能不可见，而用户只会说"点了没反应"。
            # _swallow 走 sf_logging，冻结产物里它一直可用；osascript 降级路径自身
            # 不依赖这个 import（真用不到 AppKit 的环境早在 native_available 就分出去了）。
            _swallow("sf_system_alert.show_alert(NSAlert 构造失败，降级 osascript)", exc)
    merged = text
    if message and info is not None and not text:
        merged = message + "\n\n" + info if info else message
    launch = build_launch(title, merged, buttons=buttons, default=default,
                          cancel=cancel,
                          icon_path=icon_path if icon_path and os.path.isfile(icon_path) else None)
    return SystemDialog(launch, on_result=on_result)


def show_progress_alert(message, info="", cancel="取消", on_result=None,
                        icon_path=None):
    """下载进度用的原生弹窗：NSAlert + 系统进度条；不可用时返回 None。

    与 show_alert 的关键差别是**绝不降级成 osascript**：AppleScript 的
    display dialog 装不下进度条，弹出一个"看不到进展"的框比让调用方退回
    Qt 自绘框更糟（用户会以为程序卡死）。返回 None 由调用方决定兜底。
    """
    if not native_available():
        return None
    try:
        return NativeAlert(message, info, (cancel,), None, cancel,
                           icon_path, on_result, progress=True)
    except Exception:
        return None
