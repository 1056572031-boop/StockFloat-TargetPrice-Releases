# -*- coding: utf-8 -*-
"""模块化拆分 v1.2.13：sf_glass —— 由 stock_float_window.py FloatWindow 方法搬移生成。"""

import time
import traceback
from sf_constants import (ACCENT_BLUE_HEX, APP_VERSION, BG_GLASS_DIM, BG_GLASS_DIM_LIGHT,
                          BG_LIQUID_GLASS_DIM, LIQUID_GLASS_OPACITY,
                          GLASS_INSET, GLASS_MASK_THROTTLE, R,
                          FG_BRIGHT_HEX, FG_TEXT_HEX, FG_HINT_HEX, FG_DISABLED_HEX)
from sf_logging import feature_enabled, log_line, log_swallow, recent_log_anchors, RUN_ID
from PyQt6.QtCore import QEvent, QObject, Qt, QRectF, QTimer
from PyQt6.QtGui import QColor, QGuiApplication, QPainter, QPen
from PyQt6.QtWidgets import QLineEdit, QWidget
from renderer import _glass_squircle_cgpath, smooth_round_rect_path
from sf_icons import _objc_deps

# 本模块的 pyobjc / AppKit / Quartz 全部下沉到方法内延迟 import：非 macOS、未装
# pyobjc 或 offscreen 环境下 import 本模块不失败，各条玻璃路径自行优雅降级。

# Chrome 等第三方应用进入系统全屏后会独占新的 Space；公开 NSWindow 行为无法
# 保证跨应用进入该 Space。私有 CGS 路径只作为公开行为之后的兼容补充：失败时
# 保持原有公开行为，连续失败后 60 秒再探，不造成每秒日志或资源泄漏。
_CGS_REPROBE_INTERVAL = 60.0

# 主窗/玻璃窗/工具窗统一使用的窗口层级：NSStatusWindowLevel(25)。
# 唯一来源，避免 _force_top 与稳态体检两处各写一份字面量（体检读、动作写）。
#
# 两窗同级，"谁压住谁"只由 orderFront 的先后决定。**只把玻璃层 orderFront、
# 随后不补主窗**的入口，必须走 `_reassert_top()`（v1.4.27 起，见
# stock_float_window._restore_window 的 Dock 恢复路径）；自身已经"先玻璃、后主窗"
# 成对上屏的入口（sf_glass._setup_glass）不必再加，它的顺序在同一块代码里就对了。
# 不要用"拉开层级"来排先后 —— 本机 macOS 27.0（build 26A428；此前这里写的
# "macOS 26" 是误取 26A 构建号）实测三条路都更糟：
#   · 主窗抬到 26：isOnActiveSpace() 仍返回 True，但主窗在第三方全屏 Space 里
#     整块不再合成上屏（全屏下卡片直接消失）；
#   · 玻璃降到 24（与系统菜单栏同级）：卡片压住菜单栏时菜单栏图标画在玻璃之上、
#     内容之下，顶部出现重影；
#   · 把玻璃挂成主窗 child(NSWindowBelow)：桌面上压不住内容了，但进 Chrome 全屏
#     Space 后玻璃不再合成（CGWindowList 查不到该窗），卡片失去深色底、白底白字
#     几乎不可读，且 _top_state_ok 从此永远不过 → 每拍全量置顶。
_TOP_WINDOW_LEVEL = 25

# v1.4.29 工具窗层级：主窗 + 1，唯一来源在这里（`stock_float_window._raise_tool_window`
# 只引用不写死）。为什么不能用同一个 25：同级时"谁在上"只由**最后一次 orderFront**
# 决定，而抬层之后主窗自己还会再前置若干次（`_force_top` 每 250ms 一拍、Dock 恢复、
# Space 切换）—— 实测把工具窗设成 25 时：抬层那一刻面板确实在上，但主窗再
# orderFront 一次就沉回下面。取 26 后先后由**层级**决定，与调用顺序无关：主窗再前置、
# 面板自重绘、整条 `_force_top` 重跑三条敌对序列实测都保持面板在上。
# （更正：本注释上一版把失效原因写成"Qt 把 25 异步回写成 8"，那是错的 ——
#  真因是 `_raise_tool_window` 裸调未导入的 `_objc_deps`，整段从未执行过，
#  见 stock_float_window 同名方法的文档字符串。）
# 26 相对 25 只多了"压住本程序主窗"这一件事；对普通窗口/Dock/菜单栏的关系与主窗一致
# （仍低于弹出菜单与屏保级）。
TOOL_WINDOW_LEVEL = _TOP_WINDOW_LEVEL + 1

# v1.4.26：合成顺序兜底体检的间隔。两窗同级，"谁压住谁"只由最后一次 orderFront
# 决定。已知的两个入口各有快通道兜着：玻璃层上屏后经 `_reassert_top()` 强制重置顶、
# 活动 Space 变化后下一拍重排（见 active_space_id）。这里守的是**第三种**：
# 由我们无法归因的重排造成的倒序 —— 一旦发生就是永久性的"只剩背景"空壳，
# 因为 _top_state_ok 读窗口属性看不出前后顺序，250ms 一拍的复核会一直空转。
#
# v1.4.33：5.0s → 1.0s。理由是被量出来的，不是猜的
# （tools/glass_occlusion_probe.py 当场把玻璃层 orderFront 到内容窗之上，
#  再计时看门狗多久把内容救回来）：
#     节流 5.0s → 恢复 4.46s      节流 1.0s → 恢复 0.39s
# 也就是说每次发生倒序，用户要盯着**一块空白面板最长 5 秒**。而代价是：
# 单次 CGWindowListCopyWindowInfo 实测中位 0.389ms（本机 16 条在屏窗口），
# 1 次/秒 = 占空比 0.039%，比 v1.4.10 立的那道"占空比"红线还低两个数量级。
# p99 有 4.6ms 的尾巴，但它落不进拖动手势 —— 系统拖拽期间 AppKit 在
# NSEventTrackingRunLoopMode 的模态循环里，QTimer 根本不被唤醒（v1.4.31 已量），
# 所以这一拍不会跟跟手性抢帧。
#
# v1.4.33 同时：这同一次查询现在还顺带回答"背景层真的错位了吗"
# （见 glass_main_real_state），所以这一行的间隔与功耗含义对两问共同生效。
_Z_ORDER_CHECK_INTERVAL = 1.0

# 取证日志的最小间隔（v1.4.27 复核 F5）。合成顺序复核每 1 秒一拍（v1.4.33 起，
# 原来 5 秒），判到倒序就会记一条 INFO；如果某个倒序在这台机器上修不掉，不设闸门
# 就是每天约 8.6 万行 ——
# 日志有轮转（sf_logging._maybe_rotate_log），撑不爆磁盘，但会把别的排障信息冲出
# 当前 debug.log，反而看不见真正要看的那几行。同一原因 60 秒内只留一条，被压掉
# 的条数随下一条以 "×N" 一起写出：刷不掉取证价值，只刷掉噪声。
# （复核节流从 5s 降到 1s 时这里没跟着降：查得更勤是为了**修得快**，
#   不是为了把同一个故障写得更啰嗦。）
_RETOP_LOG_INTERVAL = 60.0


# 稳态下两窗真实位置允许的最大偏差（pt）。超过即判"背景层真的错位"。
# 取 2pt 而不是 0：screencapture/WindowServer 的 bounds 是整数，四舍五入本身
# 就有 ±1pt 抖动；再留 1pt 余量，避免把量化噪声当故障反复纠正。
_Z_DESYNC_TOLERANCE_PT = 2.0

# 背景层跟随定时器的"最后活动"宽限期（秒）。见 _glass_follow_tick 的续期规则。
_GLASS_FOLLOW_GRACE = 8.0


def glass_main_real_state(main_number, glass_number):
    """向 WindowServer 问一次"用户此刻实际看到什么"：合成顺序 + 真实错位。

    返回 {"glass_on_top": bool, "desync_px": float | None}；两窗任一不在屏幕窗口
    列表里 / 异常返回 None（本轮不下结论，下一轮到点再查）。

    **两问各自独立降级**：`desync_px` 拿不到 bounds 时置 None，但顺序照判。
    合成顺序只依赖列表下标，把"缺 bounds"当成"顺序也不可信"会让 v1.4.26 那条
    空壳自愈在某些环境下整个停摆 —— 为了一个新问题弄丢一个已验证的旧能力，
    是净亏。

    为什么一个函数管两件事：这两问都需要同一份 OnScreenOnly 屏幕窗口列表，
    分开写就是每 5 秒两次 WindowServer 往返（实测单次 0.5~0.8ms）。v1.4.10 的
    省电结论靠的就是"稳态什么都不问"，所以这里**复用同一次查询**，
    新增"真实错位"这一问的边际成本是 0。
    """
    try:
        import Quartz
        infos = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID)
        want = (int(main_number), int(glass_number))
        idx = {}
        bounds = {}
        for i, info in enumerate(infos):
            n = int(info.get(Quartz.kCGWindowNumber, -1))
            if n in want:
                idx[n] = i
                bounds[n] = info.get(Quartz.kCGWindowBounds)
        if want[0] not in idx or want[1] not in idx:
            return None
        b1, b2 = bounds[want[0]], bounds[want[1]]
        desync = None
        if b1 and b2:
            desync = max(abs(float(b1["X"]) - float(b2["X"])),
                         abs(float(b1["Y"]) - float(b2["Y"])))
        return {
            # 列表越靠前越在上（与 glass_covers_main 同一口径）
            "glass_on_top": idx[want[1]] < idx[want[0]],
            "desync_px": desync,
        }
    except Exception:
        return None


def glass_covers_main(main_number, glass_number):
    """WindowServer 的实际合成顺序里，玻璃层是否挡在主窗之前。

    只在两窗都已上屏、且能在同一份屏幕窗口列表里查到时才下结论；
    任一查不到或异常一律返回 False（本轮不纠正，下一轮到点再查）——
    误判"盖住了"会让置顶白做，误判"没盖住"只是延后 5 秒，两者都不可接受，
    因此这里不做任何猜测式判断。

    v1.4.33 起是 `glass_main_real_state()` 的一层薄包装：保留这个入口是因为
    它已被用例直接调用，且"只看顺序"这个问法在别处仍然成立。
    """
    st = glass_main_real_state(main_number, glass_number)
    return bool(st and st["glass_on_top"])


_CGS_LIBS = None

# CGSMainConnectionID 的返回值在整个进程内恒定，单独缓存一次：
# 置顶复核每 250ms 要读一次活动 Space（见 active_space_id），不能每次都问。
_CGS_CONN = [None]


def active_space_id():
    """当前活动 Space 的 id；读不到一律返回 None（调用方据此**不动**）。

    用途：两窗同为 25 级，谁压住谁只由最后一次 orderFront 决定，而第三方应用
    （Chrome 等）进出系统全屏会让 WindowServer 重新排布本进程的两个窗，
    顺序可能在**我们没有任何调用**的情况下被倒过来 —— 倒过来之后
    _top_state_ok 读到的窗口属性全是"对的"，250ms 一拍的复核永远空转。
    这里用活动 Space 的变化作为"该重排一次了"的信号。

    成本：本机实测单次 0.76µs（2000 次均值），4 次/秒 = 占空比 0.0003%，
    对 v1.4.10 的省电结论无影响。
    """
    if not feature_enabled("private_spaces"):
        return None
    try:
        ctypes, cg, cf = _cgs_libs()
        if _CGS_CONN[0] is None:
            cg.CGSMainConnectionID.restype = ctypes.c_uint32
            conn = cg.CGSMainConnectionID()
            if not conn:
                return None
            _CGS_CONN[0] = conn
        cg.CGSGetActiveSpace.restype = ctypes.c_uint64
        cg.CGSGetActiveSpace.argtypes = [ctypes.c_uint32]
        return int(cg.CGSGetActiveSpace(_CGS_CONN[0])) or None
    except Exception:
        return None


def _cgs_libs():
    """CoreGraphics / CoreFoundation 的 ctypes 句柄（进程内缓存）。

    原实现在每次私有 Space 兼容路径里都 `ctypes.cdll.LoadLibrary` 两遍；dlopen
    虽有系统级引用计数，但每次仍要付一遍查找 + ctypes 对象构造。置顶复核每 250ms
    一次，全屏期间就是每秒两次重复加载。加载失败照原样抛出，由调用方 except 降级。
    """
    global _CGS_LIBS
    if _CGS_LIBS is None:
        import ctypes
        cg = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        cf = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        _CGS_LIBS = (ctypes, cg, cf)
    return _CGS_LIBS

# 子窗与主窗共用的视觉令牌。子窗不再自创深蓝不透明卡；由于短生命周期子窗
# 禁用独立 NSVisualEffectView（避免历史 objc_release 崩溃），用主窗 translucent
# 模式同款 82% 半透明黑模拟统一材质，并复用同一超椭圆圆角与透明边缘。
POPUP_BG_ALPHA = 0.82
# 弹窗材质沿用主窗的中性深色玻璃基调，仅加极弱蓝灰高光；不读取桌面像素，
# 因此不会泄露或把背后内容带进输入/目标价界面。
POPUP_SURFACE_RGB = (29, 30, 32)
POPUP_SURFACE_HIGHLIGHT_RGB = (151, 170, 196)
POPUP_SURFACE_HIGHLIGHT_ALPHA = 15
POPUP_INPUT_BG = "rgba(255,255,255,0.08)"
POPUP_INPUT_BORDER = "rgba(154,163,181,0.30)"
POPUP_INPUT_FOCUS_BORDER = "rgba(174,190,212,0.70)"
POPUP_INPUT_FOCUS_RGB = (174, 190, 212, 178)
POPUP_SECONDARY_BG = "rgba(255,255,255,0.10)"
POPUP_SECONDARY_HOVER = "rgba(255,255,255,0.18)"
POPUP_SECONDARY_PRESSED = "rgba(255,255,255,0.24)"
POPUP_PRIMARY_BG = ACCENT_BLUE_HEX   # 与右键菜单选中行同一份来源（v1.4.64，复核 F6）
POPUP_PRIMARY_HOVER = "#5c88ff"
POPUP_PRIMARY_PRESSED = "#3f6fe8"
POPUP_FG = "#efefef"
# 与 sf_constants.FG_HINT_HEX 同一个值，不再各写一份（v1.4.64，全方位复核 F6）：
# 这一族"提示小字灰"此前在 sf_editor / target_panel / 这里各存了一份字面量。
POPUP_FG_DIM = FG_HINT_HEX
POPUP_INPUT_H = 36  # 两个编辑弹窗共用输入框高度；加号按钮与之同高同宽
POPUP_TARGET_INPUT_W = 100  # 用户指定：目标价及替换股票的输入框共用宽度


class PopupLineEdit(QLineEdit):
    """编辑弹窗共用输入框：自绘抗锯齿圆角描边，避免 macOS 原生样式边缘锯齿。"""
    RADIUS = 9

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        if self.isEnabled():
            bg = QColor(255, 255, 255, 20)
            # 聚焦描边使用压暗中性灰：仍能明确识别输入状态，但不抢正文视觉层级。
            border = QColor(*POPUP_INPUT_FOCUS_RGB) if self.hasFocus() else QColor(154, 163, 181, 66)
        else:
            bg = QColor(255, 255, 255, 9)
            border = QColor(90, 96, 114, 87)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setBrush(bg)
        p.drawRoundedRect(rect, self.RADIUS, self.RADIUS)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(border, 1.0))
        p.drawRoundedRect(rect, self.RADIUS, self.RADIUS)
        p.end()
        # 文字/光标仍交给 Qt 绘制；样式表已去除背景和 border，避免重复描边。
        super().paintEvent(event)


def popup_stylesheet(extra=""):
    """两个编辑窗口共用的控件样式，避免各自硬编码后再次视觉漂移。

    v1.4.64（全方位复核 F6）：文字色一律走 sf_constants 的灰阶 token，样式表里不再
    出现裸 hex（判据见 tests/test_style_hex_tokens.py）。这一版唯一改变视觉的是
    禁用态那两枚：旧值 #5a6072（对弹窗面 2.66:1）与 #68718a（3.43:1）合成一枚
    FG_DISABLED_HEX（#80899b，4.74:1）。描边用的 rgba / RGB 元组不在这条口径内 ——
    它们承载的是"框"不是"字"，改它们等于顺手改视觉（见 PopupLineEdit.paintEvent）。
    """
    return (
        "QWidget{color:%s;background:transparent;font-family:'PingFang SC';}" % POPUP_FG
        + "QLineEdit{background:transparent;color:%s;border:none;"
          "padding:4px 9px;font-size:12px;selection-background-color:%s;}"
          % (FG_BRIGHT_HEX, POPUP_PRIMARY_BG)
        + "QLineEdit:focus{border:none;}"
        + "QLineEdit:disabled{background:transparent;color:%s;border:none;}"
          % FG_DISABLED_HEX
        + "QPushButton{border:1px solid transparent;border-radius:7px;padding:7px 16px;"
          "font-size:12px;font-weight:bold;color:%s;}" % POPUP_FG
        + "QPushButton#ok{background:%s;color:%s;}" % (POPUP_PRIMARY_BG, FG_BRIGHT_HEX)
        + "QPushButton#ok:hover{background:%s;}" % POPUP_PRIMARY_HOVER
        + "QPushButton#ok:pressed{background:%s;}" % POPUP_PRIMARY_PRESSED
        + "QPushButton#ok:disabled{background:rgba(79,124,255,0.24);color:%s;}"
          % FG_DISABLED_HEX
        + "QPushButton#cancel{background:%s;color:%s;}" % (POPUP_SECONDARY_BG, FG_TEXT_HEX)
        + "QPushButton#cancel:hover{background:%s;}" % POPUP_SECONDARY_HOVER
        + "QPushButton#cancel:pressed{background:%s;}" % POPUP_SECONDARY_PRESSED
        + "QPushButton#cancel:disabled{background:rgba(255,255,255,0.04);color:%s;}"
          % FG_DISABLED_HEX
        + extra
    )


def native_cocoa_active():
    """当前是否跑在 cocoa 上（只有 cocoa 下 `winId()` 才是 NSView*）。

    offscreen/minimal 下把 winId() 当指针转成 objc_object 得到的是无效地址，
    后面任何 `.window()` / `setLevel_()` 都会**直接段错误**（不是抛异常，兜不住）。
    凡是"从 Qt 侧伸手进 AppKit"的地方都必须先问这一句 ——
    `disable_native_window_shadow` 与 `stock_float_window._raise_tool_window` 共用本判定。
    """
    try:
        app = QGuiApplication.instance()
        return (app.platformName().lower() if app is not None else "") == "cocoa"
    except Exception:
        return False


def disable_native_window_shadow(widget):
    """关闭 Tool 窗口默认系统阴影，消除截图中的厚黑外圈；失败静默降级。"""
    try:
        # 只有 cocoa 下 winId() 才是 NSView*；offscreen/minimal 下把它当指针
        # 转成 objc_object 得到的是无效地址，后面的 .window() 直接崩。
        if not native_cocoa_active():
            return
        nsw = content_ns_window(widget)
        if nsw is not None:
            nsw.setHasShadow_(False)
            nsw.setOpaque_(False)
    except Exception:
        pass


def content_ns_window(widget):
    """内容窗的 NSWindow；平台不对 / 取不到一律返回 None（调用方据此**不动**）。

    仓库规矩：凡是"从 Qt 伸手进 AppKit"的路径，第一行必须问 `native_cocoa_active()`。
    offscreen/minimal 下 `winId()` 不是 NSView*，把它当指针喂给 `objc_object` 是
    **直接段错误而不是异常** —— 外面套多少层 try/except 都兜不住。
    实测 v1.4.32 产物在 `QT_QPA_PLATFORM=offscreen` 下 rc=139（源码运行同样复现），
    faulthandler 指向本函数的旧inline 版本：`sf_glass._force_top` →
    `object_new ← class_call`，与崩溃报告 `objc_opt_self + 8 / KERN_INVALID_ADDRESS 0x1`
    逐帧一致。日志只写到 3 行、没有 UNCAUGHT —— 表现就是"双击没反应、进程自己没了"。

    收敛成一个函数而不是每处各写两行，是因为这条判断在 4 个入口上完全相同，
    而漏掉任何一处都是一次不可捕获的崩溃。
    """
    if not native_cocoa_active():
        return None
    try:
        objc, c_void_p = _objc_deps()
        return objc.objc_object(c_void_p=int(widget.winId())).window()
    except Exception as exc:
        # 这里静默返回 None = 置顶/背景层同步**从此再也没人做**，正是本仓 §6.9
        # 说的"功能不见了但毫无痕迹"那一类，必须留痕（log_swallow 内部限流，
        # 被 _force_top 每 250ms 调也不会刷满日志）。
        log_swallow("sf_glass.content_ns_window", exc)
        return None


def prepare_popup_surface(widget):
    """应用无权限弹窗表面设置：只关闭系统阴影，不读取任何屏幕内容。"""
    disable_native_window_shadow(widget)
    widget.update()


def paint_popup_surface(widget, painter):
    """绘制与主窗统一的半透明深色玻璃表面；不读取屏幕、不新增外圈阴影。

    子窗不能安全地持有独立的 AppKit 背景窗，因此这里必须保留 Qt 侧的材质
    fallback。关键是沿用主窗的半透明强度，而不是铺一层不透明深色卡片；这样
    目标价和替换股票两个窗口在主窗口上方时仍能透出后方内容。
    """
    path = smooth_round_rect_path(0, 0, widget.width(), widget.height(), R)
    painter.fillPath(path, QColor(*POPUP_SURFACE_RGB,
                                   int(POPUP_BG_ALPHA * 255)))
    # 仅在表面内侧增加一条极弱的冷色受光边，给编辑面板与主窗相同的玻璃层次；
    # 不画到外缘，避免重新出现历史上的双层轮廓。
    inset = 1.0
    inner = smooth_round_rect_path(inset, inset,
                                   max(widget.width() - 2 * inset, 1),
                                   max(widget.height() - 2 * inset, 1), R)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setPen(QPen(QColor(*POPUP_SURFACE_HIGHLIGHT_RGB,
                               POPUP_SURFACE_HIGHLIGHT_ALPHA), 1.0))
    painter.drawPath(inner)


# v1.2.43 第3项：玻璃层圆角 mask 重建判定（目标尺寸缓存复用 + 节流合并）纯函数。
# 供 _sync_glass_frame 与单测共用，逻辑收敛单点，行为可测。
def should_rebuild_glass_mask(w, h, last_w, last_h, last_ts, now,
                              force_mask=False, throttle=GLASS_MASK_THROTTLE):
    """返回 True 才重建圆角 mask 并刷新目标尺寸/节流记录。

    - force_mask=True：关键节点（收展/拉伸结束、布局动画完成帧）强制重建一次；
    - 目标尺寸缓存复用：与上次重建尺寸一致（≤1px）时直接跳过 —— 动画/拉伸期间
      尺寸逐帧微调的中间帧不再每帧重建 CAShapeLayer 圆角 mask，消除突发卡顿；
    - 尺寸确实变化时按 throttle 节流合并：节流窗内多次变化只在下一次到期时以
      最新尺寸重建一次（合并重建），玻璃层圆角最多滞后一个节流周期。
    """
    if force_mask:
        return True
    if last_w or last_h:                      # 已有历史记录才启用缓存复用判断
        if abs(w - last_w) <= 1 and abs(h - last_h) <= 1:
            return False
    return (now - last_ts) >= throttle


# v1.2.46：玻璃层圆角 mask 更新计划（分帧/分用途）纯函数。
# 供 _sync_glass_frame 与 _glass_mask_flush 与单测共用，行为可测。
# 背景：v1.2.26/27 在 resizeEvent 里"动画/拉伸逐帧强制重建 mask"，与主窗 Qt 内容
# 走两条渲染流水线（Qt/AppKit 各自合成），快速拉伸时 mask 光栅化与主窗重绘帧错位，
# 顶部背景层出现变形与闪烁（缓慢拉伸每帧 Δ 小、无感）。v1.2.43 虽新增
# should_rebuild_glass_mask 降频判定，但 _sync_glass_frame 未接线，逐帧重建依旧。
# 本方案把"是否/何时重建"收敛为三态计划：
#   immediate —— force_mask 关键节点（拉伸结束/动画完成帧），由 resizeEvent 立即重建对齐；
#   skip      —— 目标尺寸与上次重建一致（≤1px），复用缓存零重建（动画/拉伸中间帧降频，
#                保留 v1.2.43 降频收益）；
#   pending   —— 尺寸确实变化，交由下一次 Qt 主窗实际绘制帧（paintEvent）同帧 flush，
#                mask 与主窗 Qt 圆角以同一几何进入合成帧，消除双流水线错位变形/闪烁。
# 时间节流（throttle / GLASS_MASK_THROTTLE）不再作为决策依据：pending 由"绘制帧驱动"
# 天然按帧合并（一次上屏至多重建一次），比 60ms 时间节流更精确、不错位。
def glass_mask_update_plan(w, h, last_w, last_h, now, last_ts, force_mask=False,
                           throttle=GLASS_MASK_THROTTLE):
    """返回 mask 更新计划：'immediate' | 'pending' | 'skip'。"""
    if force_mask:
        return 'immediate'
    if last_w or last_h:                      # 已有历史记录才启用缓存复用判断
        if abs(w - last_w) <= 1 and abs(h - last_h) <= 1:
            return 'skip'
    else:
        # 无历史尺寸（首帧/从未记录）：无缓存可复用，超过节流窗口则立即重建
        # （兼容 v1.2.20 既有纯节流契约），否则并入下一绘制帧合并。
        if (now - last_ts) >= throttle:
            return 'immediate'
    # 尺寸确实变化（或未超节流的首帧）→ 交由下一次 Qt 主窗绘制帧同帧 flush
    return 'pending'


# v1.2.65：系统外观 → 玻璃暗化度（纯函数 + 探测函数拆分）。
# 背景（BUG）：旧 _glass_dim 把外观探测结果缓存进 self._glass_mode，唯一失效点
# 是 stock_float_window._on_screen_changed（只接 primaryScreenChanged / screenAdded /
# screenRemoved）；macOS 深/浅色外观切换不触发任何屏幕事件，缓存永久陈旧 ——
# 浅色外观下（壁纸变亮）仍用暗色档的暗化度，白字对比度不足。
# 修复：探测结果不再跨绘制缓存，每次取值实时探测（单次 NSApp.effectiveAppearance()
# 相对整窗 fillPath 开销可忽略），彻底消除"失效时机"这一类问题。

def appearance_mode_from_name(name):
    """纯函数：NSAppearance 名称字符串 → 'dark' | 'light'。

    名称含 'Dark' / 'VibrantDark' 判为 dark，其余（含空/None）判为 light，
    与修复前逻辑逐字等价。可单测，不依赖 AppKit / Qt / 任何全局状态。
    """
    if name and ("Dark" in name or "VibrantDark" in name):
        return "dark"
    return "light"


def glass_dim_for_mode(mode):
    """纯函数：外观模式 → 毛玻璃暗化 alpha（0~255）。

    'light' 用更深的一档（BG_GLASS_DIM_LIGHT），保证浅色壁纸上白字仍有对比度。
    可单测，不依赖 AppKit / Qt / 任何全局状态。
    """
    return BG_GLASS_DIM_LIGHT if mode == "light" else BG_GLASS_DIM


def glass_surface_rim_for_mode(mode):
    """返回主窗玻璃内侧受光边颜色。

    背后的暖冷色仍完全由系统 NSVisualEffectView 采样与折射；本层只提供极弱的
    中性蓝灰轮廓，避免读取屏幕像素，同时让玻璃与深浅系统外观协调。
    """
    return (126, 145, 173, 12) if mode == "light" else (171, 190, 219, 16)


_OVERLAY_BEHAVIOR = None


def public_overlay_collection_behavior():
    """返回 Apple 公开的悬浮窗 Space/全屏行为组合。

    让窗口加入所有普通 Space、在 Mission Control 中保持位置，并作为全屏窗口的
    辅助窗口显示在同一 Space。常量自 macOS 10.7 起可用，覆盖本项目 macOS 13–27
    的正式支持范围。

    v1.4.10：结果缓存到模块级 —— 置顶复核的稳态体检每次都要拿它比对，
    常量不会变，没必要每次走一遍 import + 三次属性查找 + 两次或运算。
    """
    global _OVERLAY_BEHAVIOR
    if _OVERLAY_BEHAVIOR is None:
        import AppKit
        _OVERLAY_BEHAVIOR = (AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
                             | AppKit.NSWindowCollectionBehaviorStationary
                             | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary)
    return _OVERLAY_BEHAVIOR


# NSWindowCollectionBehavior 里决定"这块窗出现在哪片 Space"的两枚裸位。
# 写成常量而不是每次查 AppKit，是因为纯函数判据要在 offscreen（没有窗口服务）也跑得动；
# 值自 macOS 10.7 起没变过，tests/test_popup_fullscreen_space.py 里有一条与 AppKit
# 同名常量对拍的锁，改名/改值当场红。
BEHAVIOR_JOIN_ALL_SPACES = 1 << 0
BEHAVIOR_MOVE_TO_ACTIVE_SPACE = 1 << 1


def popup_join_all_spaces_behavior(current):
    """把弹层的 Space 行为换成"加入所有 Space"—— **替换**那一位，不是或上去。

    实测（2026-10-13，真 AppKit 上逐个现值试）：CanJoinAllSpaces 与 MoveToActiveSpace
    同时为真会让 AppKit 当场抛
    `NSInternalInconsistencyException: window behavior cannot be both ...`，
    所以只能把 MoveToActiveSpace 按掉。Qt 给所有顶层窗（右键菜单、目标价面板、
    编辑器、目标价气泡）的默认值是 258 = MoveToActiveSpace|FullScreenAuxiliary，
    改完是 257；主窗那套 273 本来不含 2 这一位，原样返回（所以这条对主窗是幂等的）。
    """
    return (int(current) | BEHAVIOR_JOIN_ALL_SPACES) & ~BEHAVIOR_MOVE_TO_ACTIVE_SPACE


def cgs_add_window_to_space(window_number):
    """私有 CGS：把一块窗加入**当前活动** Space。失败抛异常，由调用方决定怎么留痕。

    从 `GlassMixin._force_join_active_space` 里抽出来共用 —— 主窗与刚上屏的弹层要走
    的是同一条路，写成两份迟早一份修一份没修（本仓 §5.1 那条"清单答不了还漏几处"）。
    """
    if not feature_enabled("private_spaces"):
        raise RuntimeError("private Space compatibility is disabled")
    from ctypes import c_void_p, c_uint32, c_uint64, c_int, c_long, byref
    # v1.4.10：句柄走模块级缓存，不再每次 dlopen 两遍（见 _cgs_libs）
    ctypes, cg, cf = _cgs_libs()
    cg.CGSMainConnectionID.restype = c_uint32
    cg.CGSMainConnectionID.argtypes = []
    conn = cg.CGSMainConnectionID()
    if not conn or int(window_number) <= 0:
        raise RuntimeError("window server connection or window unavailable")
    cg.CGSGetActiveSpace.restype = c_uint64
    cg.CGSGetActiveSpace.argtypes = [c_uint32]
    space = cg.CGSGetActiveSpace(conn)
    if not space:
        raise RuntimeError("active space unavailable")
    win_value = c_uint64(int(window_number))
    space_value = c_uint64(space)
    cf.CFNumberCreate.restype = c_void_p
    cf.CFNumberCreate.argtypes = [c_void_p, c_int, c_void_p]
    cf.CFArrayCreate.restype = c_void_p
    cf.CFArrayCreate.argtypes = [c_void_p, ctypes.POINTER(c_void_p), c_long, c_void_p]
    cf.CFRelease.argtypes = [c_void_p]
    cf.CFRelease.restype = None
    refs = []

    def allocated(ref):
        if not ref:
            raise MemoryError("CoreFoundation allocation failed")
        refs.append(ref)
        return ref

    try:
        win_ref = allocated(cf.CFNumberCreate(None, 4, byref(win_value)))
        space_ref = allocated(cf.CFNumberCreate(None, 4, byref(space_value)))
        win_arr = allocated(cf.CFArrayCreate(None, (c_void_p * 1)(win_ref), 1, None))
        space_arr = allocated(cf.CFArrayCreate(None, (c_void_p * 1)(space_ref), 1, None))
        cg.CGSAddWindowsToSpaces.argtypes = [c_uint32, c_void_p, c_void_p]
        cg.CGSAddWindowsToSpaces(conn, win_arr, space_arr)
    finally:
        for ref in reversed(refs):
            if ref:
                try:
                    cf.CFRelease(ref)
                except Exception:
                    pass


def join_active_space(widget):
    """让一枚**刚上屏**的顶层窗出现在第三方全屏 Space 里；返回一句诊断，绝不外抛。

    为什么需要它（装机版 v1.4.68 上量出来的现场，2026-10-13）：浏览器全屏时右键，
    菜单那块窗**确实建出来了** —— WindowServer 里 layer 101、bounds 152x173、
    就在点击点旁边，距点击 6~9ms（两轮实测）—— 但在屏窗口列表里查不到它，
    三秒内每 20ms 采一次，0/97 在屏。用户看到的"右键没反应"就是这一格。
    主窗能浮在全屏之上，靠的是 `_force_top` 里"公开行为 + 私有 CGS"两步都做、
    还每 250ms 复核一次；弹层此前一步都没做过。

    两步都按主窗的口径来：先把 Space 行为换成含 CanJoinAllSpaces 的那一位
    （公开 API），再问 `isOnActiveSpace()`，仍不在活动 Space 才补私有 CGS 那一步。
    只量过公开那一步不够 —— 本机一个 273/level25 的 Qt 窗在第三方全屏 Space 里
    实测 `on_active_space=False`、整块不合成，而产品主窗在同一场景下看得见。
    """
    if not native_cocoa_active():
        return "skip:not_cocoa"
    try:
        nsw = content_ns_window(widget)
    except Exception as exc:
        log_swallow("sf_glass.join_active_space", exc)
        return "error:no_nswindow"
    if nsw is None:
        return "skip:no_nswindow"
    try:
        want = popup_join_all_spaces_behavior(nsw.collectionBehavior())
        if int(nsw.collectionBehavior()) != want:
            nsw.setCollectionBehavior_(want)
        if bool(nsw.isOnActiveSpace()):
            return "ok:behavior=%d" % want
        cgs_add_window_to_space(int(nsw.windowNumber()))
        return "ok:cgs_added behavior=%d" % want
    except Exception as exc:
        # 这一族失效的形状就是"功能不见了、界面上零报错"，必须留痕（§6.9）
        log_swallow("sf_glass.join_active_space", exc)
        return "error:%s" % type(exc).__name__


# 弹层补齐的节奏。**Show 那一次不够**是实测出来的（2026-10-13，源码版 + 真实 HID 右键）：
# 右键后 6~9ms 菜单窗就建好了（layer 101、bounds 152x173），Show 事件里改公开行为 +
# 调一次私有 CGS，`isOnActiveSpace()` 仍是假、在屏列表里查不到它；再补一次（第 2 拍）
# 才落到活动 Space，截图上取得菜单自己的底色 (38,43,56) —— 也就是用户终于看得见它。
# 所以这里按节拍复核，而不是"补一次就算完"。主窗本来就是靠 250ms 的看门狗活着的，
# 弹层用更短的节拍是因为它短命：30ms × 上限 50 拍 = 最多 1.5 秒，之后放弃并留一条日志。
POPUP_SPACE_TICK_MS = 30
POPUP_SPACE_MAX_TICKS = 50

_SPACE_WATCH: dict = {"timer": None, "widgets": []}


def _watch_key(widget):
    """名单里是否已有这块窗。用对象身份，不用 id()（id 会被回收后复用）。"""
    return any(w is widget for w, _tries in _SPACE_WATCH["widgets"])


def watch_active_space(widget):
    """一块刚上屏的顶层窗：先立刻补一次，没落地就按节拍继续补。

    返回值与 `join_active_space` 同形，便于判据直接断言"一次就够"与"要续补"两条路。
    """
    outcome = join_active_space(widget)
    if not native_cocoa_active() or outcome.startswith("ok:behavior"):
        return outcome          # 桌面上一次就够：不起表，不占任何节拍
    if not _watch_key(widget):
        _SPACE_WATCH["widgets"].append([widget, 0])
    if _SPACE_WATCH["timer"] is None:
        timer = QTimer()
        timer.setInterval(POPUP_SPACE_TICK_MS)
        timer.timeout.connect(popup_space_tick)
        _SPACE_WATCH["timer"] = timer
    _SPACE_WATCH["timer"].start()
    return outcome


def popup_space_pending():
    """还在续补名单里的弹层快照：[(窗体, 已补拍数)]。

    公开这一问而不是让判据去翻 `_SPACE_WATCH`：它答的是"此刻有几块弹层还没落到
    活动 Space"，正是"全屏时右键没菜单"这条报障将来在诊断包里要给的现场。
    """
    return [(w, tries) for w, tries in _SPACE_WATCH["widgets"]]


def popup_space_timer_running():
    """续补表是否在跑（名单空了必须停，不许空转）。"""
    timer = _SPACE_WATCH["timer"]
    return timer is not None and bool(timer.isActive())


def clear_popup_space_watch():
    """清空续补名单并停表。判据与自检用它做前后隔离；产品路径不调。"""
    timer = _SPACE_WATCH["timer"]
    if timer is not None:
        timer.stop()
    _SPACE_WATCH["widgets"] = []


def popup_space_tick():
    """复核名单：落地或关掉的出列，其余再补一次；名单空了就停表。

    停表这条不是洁癖：定时器不停就是每 30ms 一次空转的 AppKit 调用，
    而本仓的功耗结论要求"没有可见收益就不许多花节拍"。
    """
    kept = []
    for entry in _SPACE_WATCH["widgets"]:
        widget, tries = entry
        try:
            if not widget.isVisible():
                continue                       # 已经关了（菜单本来就短命），不留引用
            outcome = join_active_space(widget)
        except Exception as exc:
            log_swallow("sf_glass.popup_space_tick", exc)
            continue
        if outcome.startswith("ok:behavior"):
            continue                           # 落到活动 Space 了，收工
        if tries + 1 >= POPUP_SPACE_MAX_TICKS:
            log_line("popup 仍未进入活动 Space，放弃续补：%s"
                     % type(widget).__name__, "WARNING")
            continue
        entry[1] = tries + 1
        kept.append(entry)
    _SPACE_WATCH["widgets"] = kept
    if not kept and _SPACE_WATCH["timer"] is not None:
        _SPACE_WATCH["timer"].stop()


class _SpaceJoinFilter(QObject):
    """顶层窗一 Show 就补齐 —— 一处实现覆盖菜单/面板/编辑器/气泡四类。

    为什么是事件过滤器而不是四处各写一行：那四处是一份"清单"，而清单答不了
    "还有第几处漏在外面"（§3.1 里身份字段的同款教训：漏改一处不会有任何地方报错）。
    Show 事件只在窗出现时来一次，不在任何热路径上（主窗每拍绘制的路径不经过这里）。
    """

    def eventFilter(self, obj, event):
        try:
            if (event.type() == QEvent.Type.Show
                    and isinstance(obj, QWidget) and obj.isWindow()):
                watch_active_space(obj)
        except Exception as exc:
            log_swallow("sf_glass._SpaceJoinFilter", exc)
        return False


_SPACE_JOIN: dict = {"app": None, "filter": None}


def install_space_join_filter(app):
    """把弹层的 Space 补齐挂到 QApplication 上；幂等，同一个 app 只挂一次。"""
    if app is None or _SPACE_JOIN["app"] is app:
        return False
    _SPACE_JOIN["filter"] = _SpaceJoinFilter()
    app.installEventFilter(_SPACE_JOIN["filter"])
    _SPACE_JOIN["app"] = app
    return True


def space_join_filter_installed():
    """当前是否已经挂上（判据用；也是"忘了挂"唯一的自动探测点）。"""
    return _SPACE_JOIN["app"] is not None and _SPACE_JOIN["filter"] is not None


def clear_space_join_filter():
    """摘掉过滤器并清掉接线状态。判据用它做前后隔离；产品路径不调。

    为什么公开：`install_space_join_filter` 是幂等的，判据若不先清状态，
    "建完主窗才挂上"这条就会因为别的用例先建过主窗而恒真 —— 那是自指断言，
    不是判据（AGENTS §6 第 13 条同族）。
    """
    app, flt = _SPACE_JOIN["app"], _SPACE_JOIN["filter"]
    if app is not None and flt is not None:
        try:
            app.removeEventFilter(flt)
        except Exception as exc:
            log_swallow("sf_glass.clear_space_join_filter", exc)
    _SPACE_JOIN["app"] = None
    _SPACE_JOIN["filter"] = None


def detect_system_appearance():
    """读取当前系统外观：'dark' | 'light'。

    调用方：GlassMixin._glass_dim（每次绘制前调用）。仅在 macOS AppKit 可用且
    NSApp 已创建时返回真实外观，否则兜底 'dark'。异常一律兜底，不向上抛。
    """
    try:
        import AppKit
        if getattr(AppKit, "NSApp", None) is not None:
            return appearance_mode_from_name(AppKit.NSApp.effectiveAppearance().name())
    except Exception:
        pass
    return "dark"


def create_glass_surface(appkit, frame, prefer_liquid=False):
    """Create the requested native material, falling back before attaching it."""
    reason = ""
    if prefer_liquid:
        try:
            cls = getattr(appkit, "NSGlassEffectView", None)
            if cls is None:
                reason = "liquid_api_unavailable"
            else:
                view = cls.alloc().initWithFrame_(frame)
                view.setStyle_(appkit.NSGlassEffectViewStyleRegular)
                view.setCornerRadius_(float(R))
                view.setEffectIsInteractive_(False)
                view.setAppearance_(appkit.NSAppearance.appearanceNamed_(appkit.NSAppearanceNameDarkAqua))
                view.setAlphaValue_(LIQUID_GLASS_OPACITY)
                return view, "liquid", ""
        except Exception as exc:
            reason = "liquid_create_failed:" + type(exc).__name__
            log_swallow("sf_glass.create_glass_surface", exc)
    view = appkit.NSVisualEffectView.alloc().initWithFrame_(frame)
    view.setMaterial_(21)
    view.setBlendingMode_(0)
    view.setState_(1)
    return view, "legacy", reason


class GlassMixin:
        """原生毛玻璃背景层 + 强制置顶 + 弹窗材质这一族的宿主 mixin。

        组合方（三个都要求宿主同时是 QWidget）：`FloatWindow(GlassMixin, RenderMixin, QWidget)`、
        `EditorWindow(GlassMixin, QWidget)`、`TargetPanel(GlassMixin, QWidget)` —— 它自己不带
        任何 Qt 构造，全部成员挂在宿主实例上，所以**单实例化它没有意义**，也只能靠宿主提供
        `width()/height()/winId()` 之类。
        非 macOS、或 Qt 的 cocoa 平台没真起来时（`native_cocoa_active()` 为假）整族自动降级为
        Qt 侧半透明绘制：这条收口是 v1.4.33 那次"把 offscreen 的 winId() 当 NSView 指针用 →
        几秒内 SIGSEGV"之后加的，别绕开它直接从 Qt 伸手进 AppKit。
        短生命周期子窗（编辑器/目标价面板）**刻意不起独立 NSVisualEffectView**，走主窗同款
        半透明材质 —— 历史事故是 objc_release 崩溃，见本文件 POPUP_* 那段注释。

        ⚠ 类体缩进是 8 空格（历史如此），往里加类属性必须跟齐，否则整类语法错。
        """
        # 强制置顶开关：只由 _reassert_top() 在调用前后置位/复位（v1.4.27 复核 F2）。
        # 默认值放在**类上**而不是靠 getattr —— _force_top 整个方法被 try/except 兜着，
        # 读不到属性会抛 AttributeError 并被静默吞掉，置顶从此再也没人做。
        _force_top_forced = False
        # 空窗计时器的两件现场（类型在类层写死：方法里既会赋 dict 也会赋 None，
        # 让 mypy 从第一次赋值去推就会把第二处判成类型冲突）。
        # ⚠ 两张表都**只能赋 None 当类层的默认值**：`_gap_episodes = []` 写在这里
        # 就是一张跨实例共享的可变类属性，第二个窗口的场次会接在第一个后面。
        _gap_open: "dict | None" = None
        _gap_episodes: "list[dict] | None" = None
        _prefer_liquid_glass = False
        _glass_backend = "none"
        _glass_fallback_reason = ""

        def _setup_glass(self):
            """Create the independent background; retry legacy after Liquid Glass failure."""
            self._glass_backend = "none"
            self._glass_fallback_reason = ""
            nsw = content_ns_window(self)
            if nsw is None:
                self._glass_fallback_reason = "native_window_unavailable"
                return False
            try:
                import AppKit
                from Foundation import NSRect
                frame = NSRect((0, 0), (self.width(), self.height()))
                view, backend, reason = create_glass_surface(
                    AppKit, frame, getattr(self, "_prefer_liquid_glass", False))
                self._glass_backend = backend
                self._glass_fallback_reason = reason
                if self._install_glass_window(nsw, view):
                    return True
                if backend == "liquid":
                    self._glass_fallback_reason = "liquid_window_setup_failed"
                    view, backend, _reason = create_glass_surface(AppKit, frame)
                    self._glass_backend = backend
                    return self._install_glass_window(nsw, view)
                return False
            except Exception as exc:
                self._glass_backend = "none"
                self._glass_fallback_reason = "glass_setup_failed:" + type(exc).__name__
                log_swallow("sf_glass._setup_glass", exc)
                return False

        def _install_glass_window(self, nsw, vev):
            """Attach one material, with identical ownership and cleanup for both backends."""
            changed_appearance = False
            old_appearance = None
            try:
                # 只有 cocoa 下 winId() 才是 NSView*。offscreen/minimal 下把它当指针
                # 转成 objc_object 再 .window()，是**直接段错误而不是异常** —— 下面
                # 整个 try/except 一行都兜不住。实测 v1.4.32 产物在
                # QT_QPA_PLATFORM=offscreen 下 rc=139（源码运行同样复现），日志只
                # 写到"reopen handler"一行，既没有 glass ok 也没有 UNCAUGHT ——
                # 表现就是"双击没反应 / 进程自己没了"。
                # 与 disable_native_window_shadow 同一口径：先问平台，再伸手进 AppKit。
                # 平台判定收在 content_ns_window 里，与 _force_top / _sync_glass_frame
                # 同一处 —— 漏掉任何一处都是一次 try/except 兜不住的段错误。
                import AppKit
                gw = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                    nsw.frame(), 0, 2, False)  # 无边框、Buffered
                # PyObjC 持有该窗口对象；禁止 NSWindow 在 close 时自行 release，
                # 否则 AppKit 与 Python 包装对象会争夺所有权，自动释放池 drain 时崩溃。
                gw.setReleasedWhenClosed_(False)
                gw.setOpaque_(False)
                gw.setBackgroundColor_(AppKit.NSColor.clearColor())
                # 层级与 Space 行为逐位复制主窗：玻璃窗只是"垫在主窗下面的一层材质"，
                # 一旦与主窗不同层，切 Space / 全屏时就会被别的窗口夹住或整体消失。
                gw.setLevel_(nsw.level())
                gw.setCollectionBehavior_(nsw.collectionBehavior())
                gw.setIgnoresMouseEvents_(True)  # 不拦截鼠标事件
                gw.setHasShadow_(False)   # 关闭玻璃窗阴影，避免窗口外圈出现描边/暗边
                nsw.setHasShadow_(False)  # 关闭主窗原生阴影，与玻璃层保持完全一致
                if self._glass_backend == "liquid":
                    old_appearance = nsw.appearance()
                    appearance = AppKit.NSAppearance.appearanceNamed_(AppKit.NSAppearanceNameDarkAqua)
                    changed_appearance = True
                    nsw.setAppearance_(appearance)
                    gw.setAppearance_(appearance)
                self._glass_view = vev
                # 先 setContentView 再挂圆角 mask：contentView 切换会重建 view 的
                # layer，若在 setContentView 之前 setMask 会被重置，玻璃层退回直角，
                # 主窗圆角外露出第二层方形背景（用户反馈"四角两层重叠背景"）
                gw.setContentView_(vev)
                self._set_glass_mask(vev, self.width(), self.height())
                gw.orderFront_(None)
                nsw.orderFront_(None)        # 主窗置于玻璃窗之上，Qt 内容清晰显示
                self._glass_window = gw
                # orderFront 后再次确认 mask，确保出层/层状态切换未破坏圆角裁剪
                self._set_glass_mask(vev, self.width(), self.height())
                # v1.2.43 第3项：记录初始 mask 目标尺寸，供 _sync_glass_frame
                # 「同尺寸跳过重建」的目标尺寸缓存复用判定
                self._glass_mask_w = self.width()
                self._glass_mask_h = self.height()
                log_line("glass ok, backend=%s, mask=%s, fallback=%s" % (
                    self._glass_backend, "set" if self._glass_mask_layer is not None else "native",
                    self._glass_fallback_reason or "none"))
                return True
            except Exception:
                # v1.4.18 走查：若异常发生在 gw.orderFront_ 之后（如 _set_glass_mask
                # 或 log_line 磁盘 I/O 失败），这块玻璃已经上屏；下面把 _glass_window
                # 置 None 会让它彻底失去引用 —— 一块点击穿透的毛玻璃永久留在桌面上，
                # 后续 _close_glass 再也收不到它。失败收口前先把已上屏的 gw orderOut，
                # 并把主窗阴影恢复回默认（309 已无条件关掉，失败时不能让它一直裸着）。
                try:
                    if locals().get("gw") is not None:
                        gw.orderOut_(None)
                except Exception:
                    pass
                try:
                    if locals().get("nsw") is not None:
                        nsw.setHasShadow_(True)
                except Exception:
                    pass
                if changed_appearance:
                    try:
                        nsw.setAppearance_(old_appearance)
                    except Exception as exc:
                        log_swallow("sf_glass._install_glass_window.appearance", exc)
                try:
                    log_line("glass setup error: " + traceback.format_exc().strip().replace("\n", " | "))
                except Exception:
                    pass
                self._glass_window = None
                self._glass_view = None
                self._glass_mask_layer = None
                self._glass_backend = "none"
                return False

        def _init_glass_state(self):
            """v1.2.87：子窗（目标价面板 / 替换股票编辑器）复用毛玻璃时的状态初始化。

            与主窗 FloatWindow.__init__ 内联初始化保持同一组属性名与初值，
            确保 _setup_glass / _sync_glass_frame / _force_top / _glass_mask_flush
            等方法在所有消费方上行为一致。非 macOS 环境下 _setup_glass 会优雅降级，
            仅影响外观（退回半透明深色卡），不破坏任何功能。
            """
            self._bg_effect = False          # 是否已生效系统毛玻璃
            self._glass_backend = "none"
            self._glass_fallback_reason = ""
            self._glass_view = None
            self._glass_window = None        # 独立毛玻璃 NSWindow（置于本窗下层）
            self._glass_mask_layer = None    # 玻璃层圆角裁剪 CAShapeLayer
            self._glass_mask_pending = False  # 是否有待同帧 flush 的 mask 重建
            self._glass_mask_w = 0           # 最近一次重建 mask 的目标宽（尺寸复用判定）
            self._glass_mask_h = 0
            self._last_glass_mask_ts = 0.0   # 玻璃层 mask 最近重建时刻（节流用）
            self._cgs_ok = None              # 私有全屏 Space 兼容路径状态
            self._cgs_fail = 0
            self._cgs_disabled_at = 0.0      # 连续失败后暂停重探的起点
            self._glass_pending = True       # 子窗默认尝试毛玻璃；非 macOS 自动降级
            self._glass_follow_timer = None  # 系统拖拽期间的原生 run loop 定时器
            self._glass_follow_until = 0.0   # 宽限期终点（最后一次真实移动 + 8s）
            self._glass_follow_pos = None    # 上一拍看到的内容窗真实矩形（判"还在动"）
            self._glass_follow_cnum = None   # 内容窗窗口号（向 WindowServer 查实时位置）
            self._glass_follow_sh = 0.0      # 主屏高度（CG↔NS 坐标换算用）

        # ---- v1.4.31：系统拖拽期间的背景层跟随 --------------------------------
        def _content_cg_bounds(self, cnum=None):
            """内容窗此刻在屏幕上的真实矩形，**CG 全局坐标**（左上原点、y 向下）。

            为什么要单独一份 CG 口径：CG 全局坐标与 Qt 的全局坐标同原点的、同方向，
            是唯一能直接和 `self.x()/self.y()` 对拍的那一份（下面
            `_content_real_bounds` 返回的是 NS 口径，左下原点、y 向上，不能直接比）。
            """
            try:
                import Quartz
                if cnum is None:
                    nsw = content_ns_window(self)
                    if nsw is None:
                        return None
                    cnum = int(nsw.windowNumber())
                info = Quartz.CGWindowListCopyWindowInfo(
                    Quartz.kCGWindowListOptionIncludingWindow, int(cnum))
                if not info:
                    return None
                b = info[0].get("kCGWindowBounds")
                if not b:
                    return None
                return (float(b["X"]), float(b["Y"]),
                        float(b["Width"]), float(b["Height"]))
            except Exception as exc:
                log_swallow("sf_glass._content_cg_bounds", exc)
                return None

        def _reconcile_model_with_server(self, why=""):
            """把 Qt 缓存的窗口原点改成 WindowServer 眼里的真原点；不一致才动。

            为什么需要（v1.4.36，本机正式版实测）：在第三方全屏 Space 里拖动主界面后
            点「收起分时走势图」，卡片会**瞬移回拖动之前的位置** —— 实测
            (639,348) → 拖到 (939,508) → 点一下收起 → 回到 (639,348)，
            落点偏差正好等于整段手势的位移。机制：`performWindowDragWithEvent:`
            把位移交给 WindowServer，而在那个 Space 里本应用不是活动应用，Qt 缓存的
            几何没追上；下一次**写**几何（收展动画的 resize、min/max 的同步钳制）
            就把这个过时原点又推回 AppKit。桌面上同一条手势实测无此问题（松手后
            模型与服务端差 0px），所以这一问只放在"马上要写几何"的两个入口各问一次，
            不进 16ms 热路径。

            只搬原点，不动尺寸：尺寸不一致时这里只多写一行取证，真正的错位由
            `_steady_state_fault` 那条链负责，两条判据不互相顶包。
            """
            if not native_cocoa_active():
                return False
            if content_ns_window(self) is None:
                return False
            real = self._content_cg_bounds()
            if real is None:
                return False
            x, y = int(round(real[0])), int(round(real[1]))
            if (x, y) == (self.x(), self.y()):
                return False
            log_line("geom resync%s: model (%d,%d) -> server (%d,%d)%s" % (
                     ("[" + why + "]") if why else "", self.x(), self.y(), x, y,
                     "" if int(round(real[3])) == self.height()
                     else " (h 也不同=%d)" % int(round(real[3]))), "INFO")
            self.move(x, y)
            return True

        def _content_real_bounds(self, cnum=None, sh=None):
            """内容窗**此刻在屏幕上的真实矩形**（WindowServer 口径，NS 坐标）。

            为什么必须问 WindowServer 而不是读 `nsw.frame()`：真鼠标 + 同进程每 6ms
            对比实测 —— 系统接管拖动期间 AppKit 自己的模型是过时的（服务端已把内容窗
            拖走 234px，应用侧模型还停在起点，松手才更新）。所以任何"读自己 frame 去
            同步下层"的写法从原理上就追不上，且一旦模型不收敛就永久错位。

            返回 (x, y_bottom_left, w, h)；查不到（窗口不在屏 / 未上屏 / 异常）返回 None，
            由调用方退回模型口径 —— 退回旧行为不会比修前更糟。

            `cnum`/`sh` 是给 8ms 跟随 tick 用的缓存入参：两者在一段手势里恒定，
            不传则就地取（`_sync_glass_frame` 每 250ms 一次，取一次无所谓）。
            """
            try:
                import Quartz
                cg = self._content_cg_bounds(cnum)
                if cg is None:
                    return None
                x, y_top, w, h = cg
                if sh is None:
                    sh = float(Quartz.CGDisplayBounds(
                        Quartz.CGMainDisplayID()).size.height)
                # CG 原点在左上、y 向下；NSWindow frame 原点在左下、y 向上。
                # 两个坐标空间都以"主屏"为锚，所以换算只用主屏高度，与窗口在哪块屏无关。
                return (x, sh - (y_top + h), w, h)
            except Exception as exc:
                log_swallow("sf_glass._content_real_bounds", exc)
                return None

        def _glass_follow_tick(self):
            """拖拽中把背景层挪到内容窗的**实时**位置。

            为什么不能复用 `_sync_glass_frame()`：见 `_content_real_bounds` ——
            系统拖拽期间应用侧模型是过时的。三种提交方式（display=NO / display=YES /
            显式 CATransaction.flush）实测掉队完全相同（44~52px），所以方向不在
            "怎么提交"，而在"以谁为准"。改问 WindowServer 后掉队从 44px 降到 p95 8px。

            为什么间隔是 8ms 而不是更快：试过"4ms + 两次查询之间用光标增量插值"
            （系统拖拽里窗口与光标 1:1），实测 p95 9px —— **没有任何改善**。残留的
            约 1 帧是背景层位置提交给 WindowServer 的呈现延迟，不是采样不够勤。
            既然没有收益，就不为它多养一份锚点状态。

            宽限期只在"内容窗真的动了"时续期（v1.4.33）：原来 8 秒截止在
            `_glass_follow_begin` 一次算死，慢速长拖过 8 秒就悄悄不再跟随，而背景层
            从此停在半路 —— 且旧判据看不见（它比的是两个同样过时的模型）。改成按活动
            续期后，长拖不断跟；丢了 mouseRelease 时，最迟在最后一次移动 8 秒后自停，
            不会留下一个 8ms 定时器永久活着。
            """
            gw = getattr(self, "_glass_window", None)
            if gw is None:
                return
            real = self._content_real_bounds(getattr(self, "_glass_follow_cnum", None),
                                             getattr(self, "_glass_follow_sh", None))
            if real is None:
                return
            from AppKit import NSMakeRect
            gw.setFrame_display_(NSMakeRect(real[0], real[1], real[2], real[3]), False)
            if real != getattr(self, "_glass_follow_pos", None):
                prev = getattr(self, "_glass_follow_pos", None)
                if prev is not None:
                    # 掉队幅度（诊断导出用）：上一拍看到的位置与这一拍真实位置的距离。
                    # 只在位置变化时算一次（拖拽期 8ms 定时器里也只在变化帧跑），
                    # 取本次跟随会话的最大值，`_glass_follow_begin` 归零。
                    _dx = abs(real[0] - prev[0])
                    _dy = abs(real[1] - prev[1])
                    _lag = (_dx * _dx + _dy * _dy) ** 0.5
                    if _lag > getattr(self, "_glass_follow_lag_px", 0.0):
                        self._glass_follow_lag_px = _lag
                self._glass_follow_pos = real
                self._glass_follow_until = time.monotonic() + _GLASS_FOLLOW_GRACE
            elif time.monotonic() > getattr(self, "_glass_follow_until", 0.0):
                # 位置连续 8 秒没再变：手势已经丢了，收摊
                self._glass_follow_end()

        def _glass_follow_begin(self):
            """把"挪背景层"挂到原生 run loop 上，只在系统拖拽期间活着。

            为什么不能用 QTimer：`windowHandle().startSystemMove()`（cocoa 的
            performWindowDragWithEvent:）会让 AppKit 进入
            NSEventTrackingRunLoopMode 的模态事件循环，Qt 的定时器在那段时间不被唤醒。
            挂到 run loop 的 **common modes**（AppKit 把跟踪模式注册进了公共模式集）
            就能照常触发 —— 实测本定时器在拖拽全程稳定执行。
            """
            # 掉队幅度按"这一次跟随"计，开始那一刻先清零 —— 放在玻璃层判据之前，
            # 是为了让"没有玻璃层"这条路也把上一场的数字结掉（否则包里可能永远
            # 挂着一次早已结束的会话的最大值）。
            self._glass_follow_lag_px = 0.0
            if getattr(self, "_glass_window", None) is None:
                return
            try:
                from CoreFoundation import (CFAbsoluteTimeGetCurrent, CFRunLoopAddTimer,
                                            CFRunLoopGetMain, CFRunLoopTimerCreate,
                                            kCFRunLoopCommonModes)
                self._glass_follow_end()
                # 一段手势里窗口号与主屏高度都不变，先缓存下来给 8ms tick 用，
                # 免得每拍多付一次 winId()→NSWindow→windowNumber() 与一次取主屏高度。
                nsw = content_ns_window(self)
                if nsw is None:
                    return
                import Quartz
                self._glass_follow_cnum = int(nsw.windowNumber())
                self._glass_follow_sh = float(
                    Quartz.CGDisplayBounds(Quartz.CGMainDisplayID()).size.height)
                # 起点先记一次：宽限期从"最后一次移动"起算，第一次 tick 若位置没变
                # 也不会立刻把定时器收掉（刚按下时窗口本来就还没动）。
                self._glass_follow_pos = None
                self._glass_follow_until = time.monotonic() + _GLASS_FOLLOW_GRACE

                def _cb(_timer, _info):
                    try:
                        if time.monotonic() > getattr(self, "_glass_follow_until", 0.0):
                            # 丢了 mouseRelease 也不能留一个 8ms 定时器永久活着
                            self._glass_follow_end()
                            return
                        self._glass_follow_tick()
                    except Exception as exc:
                        log_swallow("sf_glass.glass_follow_tick", exc)

                timer = CFRunLoopTimerCreate(
                    None, CFAbsoluteTimeGetCurrent() + 0.008, 0.008, 0, 0, _cb, None)
                if timer is None:
                    return
                self._glass_follow_timer = timer
                CFRunLoopAddTimer(CFRunLoopGetMain(), timer, kCFRunLoopCommonModes)
            except Exception as exc:
                log_swallow("sf_glass._glass_follow_begin", exc)
                self._glass_follow_end()

        def _glass_follow_end(self):
            """停掉背景层跟随定时器（松手、或再次 begin 前的复位）。"""
            timer = getattr(self, "_glass_follow_timer", None)
            self._glass_follow_pos = None
            self._glass_follow_cnum = None
            self._glass_follow_sh = 0.0
            if timer is None:
                return
            self._glass_follow_timer = None
            try:
                from CoreFoundation import (CFRunLoopGetMain, CFRunLoopRemoveTimer,
                                            CFRunLoopTimerInvalidate,
                                            kCFRunLoopCommonModes)
                CFRunLoopTimerInvalidate(timer)
                CFRunLoopRemoveTimer(CFRunLoopGetMain(), timer, kCFRunLoopCommonModes)
            except Exception as exc:
                log_swallow("sf_glass._glass_follow_end", exc)

        def _inject_glass(self):
            """子窗统一使用 Qt 半透明面板，不再创建独立 NSVisualEffectView。

            v1.2.90 现场连续崩溃均为 NSVisualEffectView/objc_release；同一独立原生
            背景窗还会在层级切换时盖住 Qt 控件，形成“只有面板、没有内容”的空壳。
            主行情窗覆盖了本方法，仍保留系统毛玻璃；这里只影响目标价与替换股票弹窗。
            """
            self._bg_effect = False
            self._glass_window = None
            self._glass_view = None
            self._glass_mask_layer = None

            self._glass_backend = "none"
            self._glass_pending = False
            self.update()

        def _close_glass(self):
            """安全隐藏子窗毛玻璃，避免 PyObjC/AppKit 双重释放。

            macOS 现场崩溃报告连续落在 NSVisualEffectView 的 objc_release：原实现
            `gw.close()` 会让 AppKit 释放 contentView/NSWindow，随后 Python 包装对象
            在自动释放池清理时再次 release，形成 EXC_BAD_ACCESS。子窗本身关闭后只需
            orderOut 隐藏背景窗；保留 Python 强引用，由实例销毁时统一自然回收。
            """
            gw = getattr(self, "_glass_window", None)
            if gw is not None:
                try:
                    gw.orderOut_(None)
                except Exception:
                    pass
            self._bg_effect = False
            self._glass_backend = "none"
            # M9：复位"待注入"标记与 mask 尺寸缓存 —— 否则实例被复用时
            # （同一面板 close 后再 show，如"有未生效修改"的复用分支）：
            #   · _glass_pending 仍是 False → showEvent 不再注入 → 该实例
            #     之后永远走 paintEvent 的降级深蓝卡，与主窗毛玻璃外观不一致；
            #   · _glass_mask_w/h 保留旧尺寸 → mask 更新被判定为 'skip'，
            #     圆角不会按新尺寸重建（主窗变宽后尤其明显）。
            self._glass_pending = True
            self._glass_mask_w = 0
            self._glass_mask_h = 0
            self._glass_mask_pending = False
            self._glass_mask_layer = None

        def _set_glass_mask(self, vev, w, h):
            """用 CAShapeLayer 圆角 mask 裁剪玻璃层，使其与主窗圆角贴合（四角不露方角）。
            窗口尺寸变化（拉伸）时由 _sync_glass_frame 重建 path。"""
            if getattr(self, "_glass_backend", "legacy") == "liquid":
                vev.setCornerRadius_(float(R))
                return
            try:
                from Quartz import CGRectMake, CAShapeLayer, CATransaction
                # 内缩 GLASS_INSET：玻璃层边缘材质自带 1~3px 描边，整体内缩后由主窗
                # 透明边缘接管，描边被裁掉，窗口四周不再出现细线/亮边
                ins = GLASS_INSET
                # 圆角曲线必须与主窗 Qt 侧 smooth_round_rect_path（超椭圆 squircle）
                # 完全一致；用标准圆弧(CGPathCreateWithRoundedRect)会与超椭圆弧线
                # 不重合，四角出现"大圆角套小圆角"的双层圆角重叠
                path = _glass_squircle_cgpath(ins, ins, max(w - 2 * ins, 1),
                                              max(h - 2 * ins, 1), R)
                # v1.2.32(BUG1)：CAShapeLayer 的 frame/path 属 CALayer 可动画属性，
                # 不加抑制时 CoreAnimation 会对每次变更做约 0.25s 隐式插值追赶——
                # 拉伸时玻璃层从旧圆角向新圆角平滑过渡，而主窗 Qt 圆角瞬时重绘，
                # 顶部背景层在过渡期间与主窗错位/撕裂（稳定后收敛对齐）。
                # setDisableActions_(True) 后 geometry 立即生效，玻璃层与主窗逐帧同步。
                CATransaction.begin()
                # begin/commit 必须成对：中间 setPath_/setFrame_ 抛异常时若漏掉
                # commit，事务会一直挂着，后续所有 CALayer 变更都被吞掉不生效。
                try:
                    CATransaction.setDisableActions_(True)
                    if self._glass_mask_layer is None:
                        vev.setWantsLayer_(True)
                        layer = vev.layer()
                        if layer is None:
                            raise RuntimeError("vev.layer() is None")
                        self._glass_mask_layer = CAShapeLayer.layer()
                        self._glass_mask_layer.setFrame_(CGRectMake(0, 0, w, h))
                        layer.setMask_(self._glass_mask_layer)
                    # 每次重建路径时同步 mask 的 frame，避免 layer bounds 变化后 mask 错位/失效
                    self._glass_mask_layer.setFrame_(CGRectMake(0, 0, w, h))
                    self._glass_mask_layer.setPath_(path)
                finally:
                    CATransaction.commit()
            except Exception:
                # mask 失败仅导致玻璃层四角不裁圆角（方角），不影响窗口功能；
                # 记录日志便于排查，不再静默
                try:
                    log_line("glass mask error: " + traceback.format_exc().strip().replace("\n", " | "))
                except Exception:
                    pass

        def _sync_glass_frame(self, force_mask=False, use_server_origin=False):
            """主窗移动/缩放时同步玻璃窗 frame 与圆角 mask。
            v1.2.20 性能优化（拉伸卡顿主因修复）：
            - frame 始终实时同步，但 setFrame_display_ 用 display=False 交给系统合成周期
              更新，避免每次 resize 强制立即重绘玻璃层，消除与主窗内容层的不同步闪跳；
            - 圆角 mask（squircle CGPath 重建 + setPath_ 触发 CoreAnimation 重绘）成本最高，
              节流到 GLASS_MASK_THROTTLE(60ms) 一次；force_mask=True（拉伸结束等关键节点）
              强制立即重建一次，保证最终圆角精确对齐。

            `use_server_origin=True`（v1.4.33）：**位置**以 WindowServer 的真实 bounds 为准，
            **尺寸**仍取应用侧模型。用在"已经判出需要纠正"的两处（250ms 置顶复核、
            系统拖拽松手收口），因为那正是模型不可信的时刻 —— 系统接管拖动期间
            AppKit 的 `frame()` 可以比服务端落后几百像素，照模型摆就是把背景层
            又搬回半路。逐帧的 moveEvent/动画路径不传这个参数：那里模型是最新的，
            改问服务端反而会慢一帧（本仓对跟手性有专门的测量底座，不为零收益付代价）。
            尺寸不跟服务端走，是因为 mask 圆角用 `self.width()/height()` 构建，
            两者必须同源，否则拉伸动画里会出现"背景尺寸与圆角尺寸不一致"的错位。
            """
            gw = getattr(self, "_glass_window", None)
            if gw is None:
                return
            try:
                from Quartz import CATransaction
                nsw = content_ns_window(self)
                if nsw is None:
                    # 拿不到内容窗就什么都不摆：宁可不挪，也不能把背景层摆到猜的位置
                    return
                frame = nsw.frame()
                if use_server_origin:
                    real = self._content_real_bounds()
                    if real is not None:
                        from AppKit import NSMakeRect
                        frame = NSMakeRect(real[0], real[1],
                                           frame.size.width, frame.size.height)
                # v1.2.32(BUG1)：setFrame_ 同样会触发 CoreAnimation 隐式动画
                # （位置/尺寸向新值插值追赶），拉伸时玻璃窗 frame 拖影滞后于主窗，
                # 与 mask 同源。display=False 只取消强制立即重绘，不禁隐式动画，
                # 统一用 CATransaction 禁隐式动作后 frame 立即生效、逐帧对齐。
                CATransaction.begin()
                try:
                    CATransaction.setDisableActions_(True)
                    gw.setFrame_display_(frame, False)
                finally:
                    CATransaction.commit()
                vev = getattr(self, "_glass_view", None)
                if vev is not None:
                    now = time.monotonic()
                    # v1.2.46：mask 更新决策收敛为三态计划（immediate/pending/skip）。
                    # 移除 v1.2.26/27 的 animating/resizing 逐帧强制重建分支 —— 该分支在
                    # resizeEvent 里立即重建 mask，与主窗 Qt 内容（paintEvent 异步重绘 +
                    # AppKit 合成）两条渲染流水线帧错位，快速拉伸时顶部背景层变形/闪烁；
                    # 慢速拉伸逐帧 Δ≤1px 恰好被 should_rebuild_glass_mask 尺寸复用跳过、
                    # mask 不重建反而不触发 —— 与用户"快速异常、缓慢正常"现象完全对应。
                    # 现仅：
                    #   immediate（force_mask 关键节点）→ 本方法立即重建精确对齐；
                    #   pending（尺寸确实变化）→ 置标志，交由 _glass_mask_flush 在
                    #     下一次 Qt 主窗 paintEvent 同帧重建，与主窗圆角同一几何入合成帧；
                    #   skip（Δ≤1px 复用）→ 零重建，保留 v1.2.43 中间帧降频收益。
                    plan = glass_mask_update_plan(
                        self.width(), self.height(),
                        getattr(self, "_glass_mask_w", 0) or 0,
                        getattr(self, "_glass_mask_h", 0) or 0,
                        now, getattr(self, "_last_glass_mask_ts", 0.0) or 0.0,
                        force_mask=force_mask)
                    if plan == 'immediate':
                        self._set_glass_mask(vev, self.width(), self.height())
                        self._glass_mask_w = self.width()
                        self._glass_mask_h = self.height()
                        self._last_glass_mask_ts = now
                        self._glass_mask_pending = False
                    elif plan == 'pending':
                        self._glass_mask_pending = True
                    else:  # 'skip'
                        self._glass_mask_pending = False
            except Exception as _swallow_exc:
                log_swallow("sf_glass._sync_glass_frame", _swallow_exc)

        def _glass_mask_flush(self):
            """v1.2.46：Qt 主窗实际绘制帧内同帧同步玻璃层圆角 mask（pending 时）。

            由 RenderMixin.paintEvent 每次整窗/局部绘制开头调用：此时主窗 Qt 背景
            （暗化圆角）正以当前 self.width()/height() 绘制，此处用同一几何重建
            mask，玻璃层与主窗圆角以同一尺寸进入合成帧 —— 消除快速拉伸时两条渲染
            流水线（Qt 重绘 / AppKit mask 光栅化）的帧错位导致的顶部背景变形与闪烁。
            pending 未置位（尺寸未变/已对齐）时零开销，保留 v1.2.43 中间帧降频收益。"""
            if not getattr(self, "_glass_mask_pending", False):
                return
            vev = getattr(self, "_glass_view", None)
            if vev is None:
                self._glass_mask_pending = False
                return
            try:
                now = time.monotonic()
                self._set_glass_mask(vev, self.width(), self.height())
                self._glass_mask_w = self.width()
                self._glass_mask_h = self.height()
                self._last_glass_mask_ts = now
            except Exception as _swallow_exc:
                log_swallow("sf_glass._glass_mask_flush", _swallow_exc)
            # 无论重建成功与否都必须清标志：留着 True 会让之后每帧都重复重建 mask，
            # 相当于退回到逐帧重建的老卡顿，且 pending 永不收敛。
            finally:
                self._glass_mask_pending = False

        def _glass_dim(self):
            """玻璃模式暗化 alpha：按系统外观动态取值，浅色系统下提亮，保证白字对比度。

            v1.2.65：每次调用**实时探测**系统外观，不再跨绘制缓存到 _glass_mode。
            旧实现的唯一失效点是屏幕变化事件，而 macOS 深/浅色切换不发屏幕事件，
            缓存会永久陈旧；实时探测使外观切换后下一次绘制即生效。

            注：调用方（含 stock_float_window._on_screen_changed 里的
            `self._glass_mode = None`）无需再做失效复位，该赋值现为无害冗余。
            """
            if getattr(self, "_glass_backend", "legacy") == "liquid":
                return BG_LIQUID_GLASS_DIM
            return glass_dim_for_mode(detect_system_appearance())

        def _glass_surface_rim(self):
            """返回当前外观下主窗内侧玻璃受光边；每帧跟随系统外观更新。"""
            return glass_surface_rim_for_mode(detect_system_appearance())

        def _force_top(self):
            """复核并维持置顶（可被稳态体检跳过的常规入口）。

            要"无条件重做一次"请调 `_reassert_top()`，**不要**给本方法加形参：
            全仓 38 处测试/工具替身按 `lambda self: None` 签名打桩（含公共夹具
            tests/test_power_v1410._WinCase.make_win 与功耗底座
            tools/power_profile.py），一加形参就会让复用它们的用例撞上 TypeError
            （v1.4.26 复核 F2 实测发生过）。
            """
            if getattr(self, "_minimized", False) or getattr(self, "_quitting", False):
                return
            try:
                window = content_ns_window(self)
                if not window:
                    return
                gw = getattr(self, "_glass_window", None)
                self._top_checks = getattr(self, "_top_checks", 0) + 1
                # v1.4.10 功耗：稳态体检先行。
                # 本方法被 top_timer 每 250ms 调一次，原来**无条件**重做一整套动作
                # （改层级、开 CATransaction 同步玻璃窗 frame、orderFront、逐个 set
                # 属性）。实测单次 651~972µs、每秒 4 次，且每一轮都要向 WindowServer
                # 发指令 —— 这是"离屏 1.35% vs 真实窗口 5.58%"那 4 个百分点差距的
                # 主要来源。现在状态本来就对时一次系统写操作都不发。
                # 探测频率仍是 250ms，所以"浏览器切全屏后亚秒恢复置顶"完全保留。
                if not self._force_top_forced and self._top_state_ok(window, gw):
                    # 体检全过，仍可能有三件事需要动手：活动 Space 变了（第三方进出
                    # 全屏，系统替我们重排了两窗）、合成顺序已经倒了（玻璃压住内容）、
                    # 背景层真的错位了（模型互比看不出来）。三者都是"读窗口属性看不出来"
                    # 的失效，必须各查一次。
                    space_changed = self._active_space_changed()
                    fault = None if space_changed else self._steady_state_fault(window, gw)
                    if not space_changed and fault is None:
                        # 这一拍什么都正常 = 用户看得见内容 → 把还在开着的空窗场次结掉
                        self._note_gap_closed()
                        return
                    # 这条日志是**故意留下的取证**：用户报的"只剩背景"在本机三条
                    # 路径（缩 Dock 恢复、真 Chrome 全屏进出、外部 orderFront）里
                    # 只有最后一条能造出来，说明触发点与机型/时序有关。下次在别的
                    # 机器上再出现时，日志里会留下是哪一种、以及有没有被自动纠正。
                    if space_changed:
                        self._log_retop("active space changed")
                    elif fault is not None:
                        self._log_retop(fault[0], fault[1])
                self._top_fixes = getattr(self, "_top_fixes", 0) + 1
                # 置顶与全屏跟随（与已验证的「追踪股价」同款实现）：
                # 层级 NSStatusWindowLevel(25)，高于普通应用(0)/浮动窗(3)与全屏窗口(0)，
                # 低于系统 UI（Dock20/菜单栏24）；不能用屏保级(1000)——实测屏保级窗口
                # 不参与 Space 跟随，全屏 Space 下 onscreen=false 不可见。
                # behavior=257 (CanJoinAllSpaces|FullScreenAuxiliary)：加入所有 Space。
                window.setLevel_(_TOP_WINDOW_LEVEL)
                self._repair_main_interaction(window)
                # 独立毛玻璃窗跟随：保持同 level 并置于主窗之下
                if gw is not None:
                    try:
                        gw.setLevel_(window.level())
                        gw.setHidesOnDeactivate_(False)
                        gw.setCollectionBehavior_(public_overlay_collection_behavior())
                        # 能走到这一行就说明"需要纠正"已经判出来了，而判据可能是
                        # 真实错位（此时应用侧模型恰恰不可信）→ 以 WindowServer 为准摆位置
                        self._sync_glass_frame(use_server_origin=True)
                        gw.orderFront_(None)   # 先置玻璃窗，主窗随后 orderFront 在其上
                        if not gw.isOnActiveSpace():
                            self._force_join_active_space(gw)
                    except Exception as _swallow_exc:
                        log_swallow("sf_glass._force_top", _swallow_exc)
                window.orderFrontRegardless()  # 强制前置（不依赖激活状态）
                # 主窗刚被前置 → 已打开的工具窗必须跟着抬回主窗上方，否则同为 25
                # 层级时主窗会遮住目标价/编辑面板。v1.4.28：这一步原来在
                # `_sync_open_tool_windows()` 里，于是拖动的每一拍都会做一遍；现在
                # 只有"主窗真的被前置过"这一处才做（跟位仍每拍做）。
                try:
                    self._sync_open_tool_windows()
                except Exception as _swallow_exc:
                    log_swallow("sf_glass._force_top", _swallow_exc)
                try:
                    self._raise_open_tool_windows()
                except Exception as _swallow_exc:
                    log_swallow("sf_glass._force_top", _swallow_exc)
                # 公开行为仍是基础：普通 Space、Mission Control 与本应用全屏由系统负责。
                # 对 Chrome 等第三方系统全屏，再尝试私有 CGS 兼容路径；不可用时静默退回。
                for attr, val in (
                    ("setBecomesKeyOnlyIfNeeded_", True),
                    ("setCollectionBehavior_", public_overlay_collection_behavior()),
                ):
                    try:
                        getattr(window, attr)(val)
                    except Exception as exc:
                        log_swallow("sf_glass._force_top." + attr, exc)
                try:
                    if not window.isOnActiveSpace():
                        self._force_join_active_space(window)
                except Exception as _swallow_exc:
                    log_swallow("sf_glass._force_top", _swallow_exc)
                # v1.4.35：整条置顶路径的**最后一步**必须把"背景层在内容窗之下"
                # 显式交给 WindowServer。放在最后是有原因的：上面那次私有
                # CGSAddWindowsToSpaces 会把内容窗重新插入活动 Space，插入位置在
                # 背景层之下 —— 于是更早那次 `window.orderFrontRegardless()` 抬上来的
                # 顺序又被后一步抹掉。实测（本机正式版，Chrome 全屏 Space 内）：
                # 倒序一旦成立，每 250ms 一拍的完整置顶连续 11 秒压不回去
                # （日志 9 条 `re-top: glass above main`，外部旁观记到 12.71 秒倒序），
                # 而换一次 Space 立刻恢复正常 —— 说明"修"这个动作本身依赖先后顺序，
                # 而那个顺序在这条路径里被自己推翻了。
                self._order_glass_below_content(window, gw)
                # 记下"这一轮置顶是在哪个 Space 上完成的"。此后活动 Space 一变
                # （第三方应用进出系统全屏、用户切桌面），就把完整置顶重做一遍 ——
                # 因为两窗同级时"谁在上"只由最后一次 orderFront 决定，而 Space
                # 切换正是由系统替我们做这个决定的时刻。取不到 id 时保持旧值：
                # 宁可少纠一次，也不能退化成每拍全量置顶。
                sp = active_space_id()
                if sp is not None:
                    self._last_top_space = sp
            except Exception as _swallow_exc:
                log_swallow("sf_glass._force_top", _swallow_exc)

        def _reassert_top(self):
            """无条件重做一次完整置顶 —— 与 `_force_top` 同签名（不带形参）。

            为什么绕这一圈（v1.4.27 复核 F2）：主窗与玻璃窗同为 25 级，谁压住谁
            只由最后一次 orderFront 决定，所以"刚把玻璃层 orderFront 过"的调用方
            （stock_float_window._restore_window 的 Dock 恢复路径）必须跳过稳态体检
            重做一遍。但体检跳过这件事**不能靠给 _force_top 加 force 形参**来实现：
            全仓 38 处测试/工具替身按 `lambda self: None` 打桩，一旦调用方传
            force=True 就 TypeError，而 _restore_window 那一段没有异常兜底，恢复
            流程会当场断在 top_timer 重启之前（实测发生过）。改由本方法置一个
            进程内开关再常规调用 _force_top，旧替身继续退化成"什么都不做"，
            正是它一直假设的行为。
            """
            self._force_top_forced = True
            try:
                self._force_top()
            finally:
                self._force_top_forced = False

        def _log_retop(self, reason, detail=""):
            """re-top 取证日志：**每种原因各自** _RETOP_LOG_INTERVAL 秒只留一条。

            压掉的条数不丢，随下一条以 "×N" 一起写出（N = 上一段里被压掉的同类
            条数）—— "倒序在反复发生"这个事实本身仍然看得见，只是不再按 5 秒一条
            刷满日志。换了原因立刻记一条，两种失效机制不能互相压掉取证。

            v1.4.35：槽位从"全类一个"改成"按原因一个"。实测原因：正式版日志
            16:08:34~16:08:43 连着 9 条同名 `glass above main`，每条后面紧跟一条
            blind —— 一个槽位时两种原因互相把对方的计时顶掉，60 秒压重形同虚设，
            "这件事在这一分钟里发生了 40 次"的强度信息全丢在刷屏里。
            分槽后同一原因的计数不再被相邻的另一种原因清零。

            `detail`（v1.4.33）**不进压重键**，只跟着打出来：错位幅度是"每次都不一
            样"的数字，进了键就等于每 5 秒一条新日志，压重形同虚设；不进键又完全
            看不见，所以取"键压重、值随行"。
            """
            now = time.monotonic()
            slots = getattr(self, "_retop_slots", None)
            if slots is None:
                slots = {}
                self._retop_slots = slots
            last = slots.get(reason)
            if last is not None and now - last["at"] < _RETOP_LOG_INTERVAL:
                last["suppressed"] += 1
                return
            pending = last["suppressed"] if last is not None else 0
            slots[reason] = {"at": now, "suppressed": 0}
            # 空窗/错位的**持续时长**从这一刻起算（v1.4.33 那条"这次空窗几秒"的缺口：
            # `_retop_slots` 只有末次时刻与压掉条数，能看出"又在发生"，看不出"那一场上
            # 一次持续了多久"——而用户报的正是"卡了好一会儿才回来"）。
            self._note_gap_open(reason)
            log_line("re-top: %s%s%s" % (reason, detail,
                                         " ×%d" % pending if pending else ""),
                     "INFO")

        def _note_gap_open(self, reason):
            """记下"这一场失效从什么时候开始"。已经在计时就不动起点。"""
            if not getattr(self, "_gap_open", None):
                self._gap_open = {"reason": reason, "since": time.monotonic()}

        def _note_gap_closed(self):
            """这一拍一切正常 → 把还在开着的那场结掉，留最近 3 场的时长。

            为什么必须在"体检全过"那条早退路上调：那是唯一能证明"用户现在看得见内容"
            的时刻；放在别处（比如纠正动作之后）只会量到"我动手了"，量不到"修好了"。
            """
            open_rec = getattr(self, "_gap_open", None)
            if not open_rec:
                return
            episodes = getattr(self, "_gap_episodes", None)
            if episodes is None:
                episodes = []
                self._gap_episodes = episodes
            episodes.insert(0, {"reason": open_rec["reason"],
                                 "duration_sec": round(
                                     max(0.0, time.monotonic() - open_rec["since"]), 2)})
            del episodes[3:]
            self._gap_open = None

        def _gap_evidence(self):
            """空窗现场：还在开着的那场（含已持续秒数）+ 最近 3 场的时长。"""
            open_rec = getattr(self, "_gap_open", None)
            out = {"open": ({"reason": open_rec["reason"],
                             "age_sec": round(
                                 max(0.0, time.monotonic() - open_rec["since"]), 2)}
                            if open_rec else None),
                   "last": list(getattr(self, "_gap_episodes", None) or [])}
            return out

        def _active_space_changed(self):
            """活动 Space 与"上次完成置顶时"是否已不同。

            返回 True 时顺手把合成顺序复核的节流清零，让**下一拍（250ms 后）**
            就真查一次 WindowServer 的前后顺序：Space 切换的重排可能在我们这轮
            置顶之后才落定，只重排一次不够，必须再看一眼。
            """
            sp = active_space_id()
            if sp is None:
                return False
            last = getattr(self, "_last_top_space", None)
            if last is None or last == sp:
                self._last_top_space = sp
                return False
            self._last_z_order_check = 0.0
            return True

        def _top_state_ok(self, window, gw):
            """置顶/全屏跟随状态是否已经正确 —— 正确则本轮不必惊动系统。

            判定刻意**保守**：任何一项读不出来、报错、或与期望值不符，一律判
            "需要重做"。漏判一次只是白干 700µs，误判成"已经对了"会让窗口不置顶
            或玻璃层错位，那是用户看得见的故障，不可接受。

            只读 5~7 个属性（约 100µs 量级），相比完整路径的十几次 objc 写调用
            加一轮 CATransaction 加 WindowServer 往返，是数量级的差别。
            """
            try:
                if int(window.level()) != _TOP_WINDOW_LEVEL:
                    return False
                if not window.isVisible():
                    return False
                if window.hidesOnDeactivate() or window.ignoresMouseEvents():
                    return False
                if not window.isOnActiveSpace():
                    return False
                if int(window.collectionBehavior()) != public_overlay_collection_behavior():
                    return False
                if gw is not None:
                    # 玻璃层必须与主窗同层、同 frame、同 Space，否则就是可见的错位
                    if not gw.isVisible():
                        return False
                    if int(gw.level()) != int(window.level()):
                        return False
                    if gw.frame() != window.frame():
                        return False
                    if not gw.isOnActiveSpace():
                        return False
            except Exception:
                return False
            # 工具窗开着**不再**单独判"需要重做"（v1.4.28，原来这里 visible 就
            # return False）。理由：
            #   · 它存在的意义是"每 250ms 借完整路径把面板抬回主窗之上"，但抬层
            #     的真正触发点是"主窗刚被 orderFront"，而那就在完整路径里，已由
            #     `_raise_open_tool_windows()` 就地补上；
            #   · 实测该分支让面板/编辑器一开着就每 250ms 白跑一次完整置顶，
            #     单次 2.6~3.5ms、p95 5.8ms、偶发 10~12.7ms，正好落在用户拖动/
            #     输入的时刻，表现为"偶尔黏一下"。
            # 兜底不变：Space 变了（`_active_space_changed`）与"用户实际看到的顺序/
            # 位置"（`_steady_state_fault`，5s 一次）都在这条"体检通过"分支后面各查
            # 一次；任何一项读不出来/异常一律判需要重做。
            #
            # 注意本方法**看不出**"两窗模型一致但都没跟上服务端"—— 它比的两个
            # frame 都来自应用侧，同一份过时不算过时。那一条由
            # `_steady_state_fault` 向 WindowServer 问，别把它加回这里。
            return True

        def _repair_main_interaction(self, window):
            """Repair native drift without changing the transparent-pixel hit-test mode."""
            for getter, setter in (("hidesOnDeactivate", "setHidesOnDeactivate_"),
                                   ("ignoresMouseEvents", "setIgnoresMouseEvents_")):
                try:
                    if getattr(window, getter)():
                        self._log_retop("native drift: " + getter)
                        getattr(window, setter)(False)
                except Exception as exc:
                    log_swallow("sf_glass._repair_main_interaction." + getter, exc)

        def _glass_real_state(self, window, gw):
            """节流版"用户实际看到什么"复核：每 _Z_ORDER_CHECK_INTERVAL 秒一次查询。

            一次 OnScreenOnly 查询同时回答合成顺序与真实错位两问（见
            `glass_main_real_state`），所以新增那一问的边际成本是 0 —— 不额外
            增加 WindowServer 往返，v1.4.10 的省电结论不受影响。
            节流未到期 / 无玻璃层 / 查不到 → None（本轮不下结论）。

            v1.4.35：顺手记一位 `_z_probe_asked` —— **"这轮没到节流、什么都没问"**
            与"**问了但查不到这一对窗**"是两件事，都返回 None 时调用方无法区分。
            此前 `_steady_state_fault` 把两者混为一谈，于是每 250ms 一拍里有三拍在
            写"order probe blind: pair not both on-screen"，而那一拍根本没发过查询
            （实测：每条 `glass above main` 之后恰好 250ms 必跟一条 blind，而节流
            是 1000ms）。一条永不失败的"我没看见"比没有这条日志更糟 —— 它会把排查
            引向"窗口真的不在屏上"，而真相是"还没轮到问"。
            """
            self._z_probe_asked = False
            if gw is None:
                return None
            now = time.monotonic()
            if now - getattr(self, "_last_z_order_check", 0.0) < _Z_ORDER_CHECK_INTERVAL:
                return None
            self._last_z_order_check = now
            self._z_probe_asked = True
            try:
                return glass_main_real_state(int(window.windowNumber()),
                                             int(gw.windowNumber()))
            except Exception:
                return None

        def _steady_state_fault(self, window, gw):
            """稳态体检通过之后，还要问 WindowServer 的那两类失效。

            返回 (原因, 附加说明) 或 None（一切正常）。

            为什么必须问服务端：`_top_state_ok` 里的 `gw.frame() != window.frame()`
            比的是**两个应用侧模型**。而系统接管拖动时模型本身就是过时的，于是
            "内容窗被拖走了、背景层留在原地"这种用户一眼就看得见的故障，在模型
            眼里可以是"两窗完全一致"—— 本机实测：真实错位 11px 的那些拍里
            `_top_state_ok` 照样返回 True（tools/glass_desync_audit.py 的
            blind_samples）。判据看不见，纠正链就永远不会启动，错位成为永久状态。

            拖拽期间不判错位：那时背景层由 8ms 原生跟随定时器负责，允许 ≤1 帧的
            正常滞后（实测 p95 8px），在这里报警只会把每 250ms 的复核变成抢它的活。
            """
            if getattr(self, "_glass_follow_timer", None) is not None:
                return None
            st = self._glass_real_state(window, gw)
            if st is None:
                # 只有"真的发了查询、却没查到这一对窗"才叫盲区。节流没到期时
                # `_glass_real_state` 同样返回 None，但那一拍什么都没问 —— 给它写
                # 一条"查不到"是说假话（v1.4.35，实测每条 glass above main 后
                # 250ms 必跟一条 blind，而节流是 1000ms）。
                if getattr(self, "_z_probe_asked", False):
                    self._log_blind(window, gw)
                return None
            if st["glass_on_top"]:
                return ("glass above main", "")
            desync = st["desync_px"]
            if desync is not None and desync > _Z_DESYNC_TOLERANCE_PT:
                return ("glass misaligned", " (%.0fpx)" % desync)
            return None

        def _log_blind(self, window, gw):
            """留痕：属性说"两窗都在活动 Space 上"，但屏幕窗口列表里找不到这一对。

            为什么这是必须写日志的一种状态而不是"正常的一拍没查到"：
            `_steady_state_fault` 只在 `_top_state_ok` 已经判"属性全对"之后才被调用，
            而 `_top_state_ok` 里包含 `isVisible()` 与 `isOnActiveSpace()` 两项。
            也就是说此刻程序认为两窗都好好在屏上，WindowServer 的在屏列表里却没有它们 ——
            这两件事同时成立只能是其中一侧在说谎。而看门狗判"查不到就不下结论"，
            于是**顺序倒了也永远不会被发现、永远不会被纠正，且日志上一行都不留**，
            正是"用户看得见故障、系统毫无痕迹"那一类。
            这里只留痕、不强行纠正：拿不到事实时乱动比不动更糟，
            但"我拿不到事实"这件事本身必须看得见。60 秒压一条，不会刷屏。
            """
            try:
                self._log_retop("order probe blind: pair not both on-screen",
                                " (main=%s glass=%s)" % (getattr(window, "windowNumber",
                                                                  lambda: "?")(),
                                                         getattr(gw, "windowNumber",
                                                                 lambda: "?")()))
            except Exception as exc:
                log_swallow("sf_glass._log_blind", exc)

        def _glass_covers_main(self, window, gw):
            """节流版合成顺序复核：玻璃层是否压住了内容窗。

            v1.4.33 起是 `_steady_state_fault` 的一部分（同一次查询、同一个节流），
            本方法保留给用例与外部按"只看顺序"这个口径直接问。
            """
            st = self._glass_real_state(window, gw)
            return bool(st and st["glass_on_top"])

        def _order_glass_below_content(self, window, gw):
            """让 WindowServer 保证"背景层在这条内容窗之下"，而不是赌谁最后 orderFront。

            为什么需要它：两窗同为 25 级，同级时"谁压住谁"只由最后一次 orderFront
            决定 —— 这是本仓反复记录过的那条设计弱点。`orderWindow:relativeTo:`
            把这条不变量**直接说给 WindowServer**，与调用先后无关，因此不会被
            同一条路径里后发生的 Space 插入抹掉（见 `_force_top` 末尾的调用注释）。

            为什么只在"已经判出需要纠正"的完整路径末尾做一次，而不是每 250ms 都做：
            稳态体检通过时整条 `_force_top` 直接 return，本方法一次都不发 ——
            v1.4.10 的省电结论（稳态什么都不问、什么都不写）不受影响。

            为什么内容窗不可见时什么都不做：参照窗不在屏时这条相对摆放会把背景层
            放到猜的位置，宁可不挪。
            """
            if window is None or gw is None:
                return
            try:
                if not window.isVisible():
                    return
                import AppKit
                gw.orderWindow_relativeTo_(AppKit.NSWindowBelow,
                                           int(window.windowNumber()))
            except Exception as exc:
                # 这条路径失败 = 背景层还可能压着内容，且"只剩背景"正是用户报的故障，
                # 必须留痕（§6.9）。
                log_swallow("sf_glass._order_glass_below_content", exc)

        def _force_join_active_space(self, window):
            """私有 CGS 兼容：把主窗或玻璃背景加入当前活动 Space。

            仅用于第三方应用的系统全屏空间；任何异常都不得影响公开置顶行为。
            连续三次失败暂停 60 秒，之后自动重探，避免永久失效与高频噪声。
            """
            now = time.monotonic()
            if getattr(self, "_cgs_ok", None) is False:
                disabled_at = getattr(self, "_cgs_disabled_at", 0.0) or 0.0
                if now - disabled_at < _CGS_REPROBE_INTERVAL:
                    return
                self._cgs_ok = None
                self._cgs_fail = 0
                log_line("cgs re-probe: %ds elapsed, retrying active space" %
                         int(now - disabled_at), "INFO")
            try:
                cgs_add_window_to_space(int(window.windowNumber()))
                self._cgs_fail = 0
                self._cgs_ok = True
            except Exception as exc:
                log_swallow("sf_glass._force_join_active_space", exc)
                self._cgs_fail = getattr(self, "_cgs_fail", 0) + 1
                if self._cgs_fail >= 3:
                    self._cgs_ok = False
                    self._cgs_disabled_at = time.monotonic()

        def glass_diagnostics(self):
            """玻璃层现场：合成顺序 + 真实错位 + 最近锚点，供「导出诊断包」读取。

            为什么必须有（2026-10-09，台账第八节第一条）：「只剩背景 / 内容看不见」这一类
            8 条线上逃逸的共同点是**现场只在用户那台机器上**。本机日志已经能给到
            "一次 1278 秒、482 行倒序检出"这种强度，但用户机器上的 debug.log 没有回传通路，
            D-04 / D-31 至今只能写"未确认，需现场取证"——这一类的定位时长因此永远记不到。
            诊断包（右键「导出诊断包」→ 桌面 zip → 发给别人排障）是**已有**的外发通路，
            所以现场并到这里，而不是新加一个菜单项，更不是偷偷上传。

            三条口径坑，都写进返回值而不是只写在注释里：
              · 非 cocoa 进程绝不碰 AppKit（`winId()` 不是 NSView 指针，伸手进去是**不可
                捕获的段错误**，v1.4.33 收口），此时 real_state 为 None 并说明为什么；
              · 诊断要的是**此刻**的答案，所以这里直调 `glass_main_real_state`，
                不复用 `_glass_real_state` 的节流 —— 节流未到期它返回 None，
                把"还没轮到问"读成"查不到"正是 v1.4.35 修掉的那类假话；
              · 锚点是**程序检出的时刻**，早于用户第一次注意到现象，别当报障时刻用。
            """
            out = {"version": APP_VERSION, "run": RUN_ID,
                   "native_cocoa": native_cocoa_active(),
                   "main_interaction": None,
                   "glass_present": getattr(self, "_glass_window", None) is not None,
                   "follow_timer_active": getattr(self, "_glass_follow_timer", None) is not None}
            backend = getattr(self, "_glass_backend", "none")
            out["surface"] = {"backend": backend,
                              "fallback_reason": getattr(self, "_glass_fallback_reason", ""),
                              "opacity": LIQUID_GLASS_OPACITY if backend == "liquid" else 1.0,
                              "dim_alpha": self._glass_dim()}
            if not out["native_cocoa"]:
                out["real_state"] = None
                out["real_state_note"] = ("非 cocoa 进程（offscreen/minimal）不碰 AppKit，"
                                          "这一项只在装机版里有值")
            else:
                try:
                    gw = getattr(self, "_glass_window", None)
                    win = content_ns_window(self)
                    if win is not None:
                        out["main_interaction"] = {
                            "hides_on_deactivate": bool(win.hidesOnDeactivate()),
                            "ignores_mouse_events": bool(win.ignoresMouseEvents()),
                            "level": int(win.level()),
                            "collection_behavior": int(win.collectionBehavior()),
                        }
                    if gw is None or win is None:
                        out["real_state"] = None
                        out["real_state_note"] = ("取不到玻璃层或内容窗的 NSWindow"
                                                  "（玻璃层未建/已降级）")
                    else:
                        main_n = int(win.windowNumber())
                        glass_n = int(gw.windowNumber())
                        out["numbers"] = {"main": main_n, "glass": glass_n}
                        # 此刻活动在哪个 Space（第三方全屏那一族的现场）：坐标跳变、
                        # "只剩背景"都只在某个 Space 上发生，而包里此前没有任何东西
                        # 说明这一拍到底在不在全屏空间里。
                        out["active_space"] = active_space_id()
                        out["in_fullscreen_space"] = bool(
                            out["active_space"] is not None
                            and out["active_space"] != getattr(self, "_normal_space_id", None))
                        out["space_note"] = ("_normal_space_id 为 None 时 in_fullscreen_space "
                                             "不可信（还没记过普通 Space 的基线）")
                        out["real_state"] = glass_main_real_state(main_n, glass_n)
                        if out["real_state"] is None:
                            out["real_state_note"] = ("问了 WindowServer，这一对窗不在屏幕窗口"
                                                      "列表里 —— 就是盲区，看门狗也看不见它们")
                except Exception as _exc:
                    log_swallow("sf_glass.glass_diagnostics", _exc)
                    out["real_state"] = None
                    out["real_state_note"] = "现场查询失败，原因已按 SWALLOWED 记进日志"
            # 每种失效最后一次是什么时候（age 秒）与压掉多少条：这是程序自己的计数，
            # 与日志锚点互为对照 —— 两边不一致时，说明有一侧在说谎（v1.4.35 的 blind 假话
            # 就是这么被抓到的）。单调时钟差值换算成秒，不外泄绝对时刻。
            slots = getattr(self, "_retop_slots", None) or {}
            now = time.monotonic()
            out["retop_last"] = {reason: {"age_sec": round(max(0.0, now - rec["at"]), 1),
                                          "suppressed": int(rec.get("suppressed", 0))}
                                 for reason, rec in slots.items()
                                 if isinstance(rec, dict)}
            out["anchors"] = recent_log_anchors()
            # 空窗持续时长（1.4.33 那条"这次空窗几秒"的缺口）：还在开着的那场 +
            # 最近 3 场各自持续多久。`retop_last` 只有末次时刻与压掉条数，
            # 它能说"又在发生"，说不出"上一场卡了多久" —— 而用户报的是后者。
            out["gap"] = self._gap_evidence()
            return out
