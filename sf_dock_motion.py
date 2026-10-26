"""收起到 Dock / 从 Dock 恢复时的过渡动画（用户 2026-10-23 要求）。

悬浮窗是无边框置顶面板：收起靠 hide()、恢复靠 show()，系统不会替它做缩放动画。
这里不碰真实窗口：收起前把「玻璃层 + 内容窗」合成截一张图，放进一块临时的透明「舞台窗」，
由舞台窗里的一层图层朝程序坞里本程序的图标飞去、缩小、淡出；恢复时反过来，落地后
先显示真实窗口，再让舞台窗的图层淡出（溶解），最后撤掉舞台窗。

四条口径（别放宽）：
① 动画只是装饰。真实窗口的收起状态在动画开始前就已落定，任何一步失败都退回
   原来的瞬间收起或恢复，并留一条 WARNING，不许让用户的窗口因为动画出不来；
② 动画交给 Core Animation：图层动画提交后由系统合成进程按显示器节拍播放，主线程只
   负责起点和终点各一次。旧写法让主线程每 16 毫秒摆一次窗口，定时器与 60Hz 屏幕刷新
   错拍，主线程一忙就掉帧；
③ 目标位置是估算值，不是读出来的：读 Dock 图标坐标要「辅助功能」授权，产品不该为了
   一段 0.2 秒的动画去向用户要权限。估算只在底部程序坞下按槽位算，其余方向落在边缘中点；
④ 人脸守卫的自动收起为了隐私必须立刻消失，调用方不走动画。
"""
from __future__ import annotations

import threading
from collections import namedtuple

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QCursor, QGuiApplication

from sf_logging import log_line, log_swallow

COLLAPSE_SEC = 0.15          # 缩小：利落，点下去立刻见到动静（2026-10-23 由 0.22 缩短）
EXPAND_SEC = 0.18            # 放大：比缩小多一点，让落地不显得生硬（由 0.26 缩短）
DISSOLVE_SEC = 0.07          # 放大落地后，舞台层盖在真实窗口上淡出的时长（由 0.10 缩短）
FINISH_SLACK = 0.03          # 定时器比动画多等一点，免得最后一帧还没播完就撤舞台
PREWARM_DELAY_MS = 4000      # 玻璃层建好后多久做一次后台预热截图
STAGE_PAD = 8.0              # 舞台窗在起终点外包矩形之外再多留的边
# 以下三个数来自本机实测（tilesize=40 时 Dock 图标间距 42、分隔线宽 18.9、两端内边距 2.5）
DOCK_PITCH_EXTRA = 2.0       # 图标间距 = 图标边长 + 2
DOCK_SEPARATOR_RATIO = 0.45  # 分隔线宽度 / 图标间距
DOCK_EDGE_PAD = 2.5
DOCK_DEFAULT_TILE = 64.0     # 用户从没动过 Dock 大小时系统的默认值
DOCK_AUTOHIDE_EXTRA = 20.0   # 自动隐藏时读不到厚度，按「图标边长 + 20」估
FINDER_ID = "com.apple.finder"


# ---------------- 纯算法：Dock 图标位置估算（坐标一律是 Qt 的左上原点） ----------------
def dock_inset(geometry, available):
    """从屏幕全幅与可用区域推断 Dock 在哪条边、厚度多少；返回 (边, 厚度)。

    自动隐藏时可用区域不让出空间，厚度读成 0，此时按底部处理，厚度由调用方估。
    """
    gx, gy, gw, gh = geometry
    ax, ay, aw, ah = available
    insets = {"bottom": (gy + gh) - (ay + ah), "left": ax - gx, "right": (gx + gw) - (ax + aw)}
    edge = max(insets, key=lambda name: insets[name])
    thick = float(insets[edge])
    return (edge, thick) if thick > 0 else ("bottom", 0.0)


def locate_slot(pinned, my_ids, extras):
    """本程序图标前面有多少个槽位、整段应用区共多少个槽位。

    pinned：固定在 Dock 里的 [(bundle_id, 槽位数)]，Finder 永远在第 0 格所以不在其中；
    extras：没固定、却正在运行的其他常规应用个数。没固定的本程序排在它们之后。
    """
    running = 1.0
    mine = None
    for bundle_id, units in pinned:
        if mine is None and bundle_id in my_ids:
            mine = running
        running += units
    if mine is None:
        mine = running + extras
        running += extras + 1.0
    else:
        running += extras
    return mine, running


def estimate_icon_rect(geometry, edge, thickness, tile, slot=None):
    """Dock 里本程序图标的估算矩形 (x, y, w, h)。

    slot=(图标前槽位数, 应用区总槽位数, 尾部槽位数) 只对底部程序坞有意义；给 None
    或在左右两侧时落在边缘中点。尾部槽位 = 分隔线后的文件夹个数 + 废纸篓。
    """
    gx, gy, gw, gh = geometry
    tile = float(tile)
    if thickness <= 0:
        thickness = tile + DOCK_AUTOHIDE_EXTRA
    if edge == "left":
        return (gx + thickness / 2.0 - tile / 2.0, gy + gh / 2.0 - tile / 2.0, tile, tile)
    if edge == "right":
        return (gx + gw - thickness / 2.0 - tile / 2.0, gy + gh / 2.0 - tile / 2.0, tile, tile)
    top = gy + gh - thickness / 2.0 - tile / 2.0
    pitch = tile + DOCK_PITCH_EXTRA
    if slot is None:
        return (gx + gw / 2.0 - tile / 2.0, top, tile, tile)
    before, apps, tail = slot
    width = 2 * DOCK_EDGE_PAD + (apps + tail) * pitch + pitch * DOCK_SEPARATOR_RATIO
    first = gx + (gw - width) / 2.0 + DOCK_EDGE_PAD
    return (first + before * pitch + (pitch - tile) / 2.0, top, tile, tile)


def refine_with_cursor(rect, cursor, geometry, thickness, magnified_size):
    """鼠标就停在程序坞上时（多半正是点图标触发的恢复），图标被放大镜顶起来，
    槽位估算会偏；此时把落点的横向中心对到鼠标上。离估算位置太远就不信这个提示。"""
    gx, gy, gw, gh = geometry
    cx, cy = cursor
    strip = max(float(thickness), float(magnified_size)) + 10.0
    if cy < gy + gh - strip or cy > gy + gh:
        return rect
    mid = rect[0] + rect[2] / 2.0
    if abs(cx - mid) > 2.0 * float(magnified_size):
        return rect
    return (cx - rect[2] / 2.0, rect[1], rect[2], rect[3])


# ---------------- 纯算法：飞行计划（图层动画的起点、终点与曲线） ----------------
# 曲线是三次贝塞尔的两个控制点 (c1x, c1y, c2x, c2y)，起点 (0,0)、终点 (1,1)。
# 这些值是按"先快后缓"的常见手感定的，没有逐帧目测调过；改它们先跑
# 测试里的单调性与"时间反转恰好镜像"两条判据。
# 轨迹必须是直线（用户 2026-10-23 要求，此前是弧线）：窗口中心的横、纵位置共用同一条
# 曲线，两个方向的进度每一刻都相等，中心点就只在起点到终点的连线上走。旧写法横向用
# 起步快的曲线、纵向用起步缓的曲线，进度不同步，轨迹才拐成弧。要改手感只换这一条曲线，
# 不许再给横纵各配一条。
CURVE_LEAD = (0.20, 0.00, 0.10, 1.00)   # 起步快、长长地收尾
CURVE_SOFT = (0.42, 0.00, 0.58, 1.00)   # 标准缓入缓出（只管纵向缩放，不影响轨迹）
FADE_HOLD = 0.55                        # 缩小时不透明保持到这个进度，之后淡出到 0

Flight = namedtuple("Flight", "stage size duration closing centers scales fade curves")


def reverse_curve(curve):
    """时间反转后的贝塞尔控制点：用它播从终点飞回起点，轨迹与原曲线恰好镜像。"""
    c1x, c1y, c2x, c2y = curve
    return (1.0 - c2x, 1.0 - c2y, 1.0 - c1x, 1.0 - c1y)


def plan_flight(window, icon, closing):
    """窗口 (x,y,w,h) 与 Dock 图标矩形之间的一次飞行。坐标均为 Qt 左上原点。

    舞台窗取两个矩形的外包框再外扩 STAGE_PAD：飞行中途横纵缩放快慢不同，
    图层的外沿可能短暂超出两端矩形，留边免得被舞台窗裁掉一角。
    centers / scales 是 (起点, 终点)；centers 为舞台窗内的局部坐标（左上原点）。
    """
    left = min(window[0], icon[0]) - STAGE_PAD
    top = min(window[1], icon[1]) - STAGE_PAD
    right = max(window[0] + window[2], icon[0] + icon[2]) + STAGE_PAD
    bottom = max(window[1] + window[3], icon[1] + icon[3]) + STAGE_PAD
    stage = (left, top, right - left, bottom - top)
    win_c = (window[0] + window[2] / 2.0 - left, window[1] + window[3] / 2.0 - top)
    icon_c = (icon[0] + icon[2] / 2.0 - left, icon[1] + icon[3] / 2.0 - top)
    icon_s = (icon[2] / window[2], icon[3] / window[3])
    curves = {"x": CURVE_LEAD, "y": CURVE_LEAD, "sx": CURVE_LEAD, "sy": CURVE_SOFT}
    if closing:
        return Flight(stage, (window[2], window[3]), COLLAPSE_SEC, True, (win_c, icon_c),
                      ((1.0, 1.0), icon_s), ((1.0, 1.0, 0.0), (0.0, FADE_HOLD, 1.0)), curves)
    back = {key: reverse_curve(value) for key, value in curves.items()}
    return Flight(stage, (window[2], window[3]), EXPAND_SEC, False, (icon_c, win_c),
                  (icon_s, (1.0, 1.0)), ((0.0, 1.0, 1.0), (0.0, 1.0 - FADE_HOLD, 1.0)), back)


# ---------------- 原生后端：只有这一段会伸手进 AppKit / Quartz ----------------
Stage = namedtuple("Stage", "window layer")


class CocoaBackend:
    """截图、舞台窗、Dock 偏好的真实实现。控制器只通过这些方法碰原生，测试里整体换成假的。

    仓库规矩：伸手进 AppKit 之前必须先问 native_cocoa_active()，由调用方（主窗的
    _dock_motion）负责；本类假定已经在 cocoa 下。所有 import 都在函数里，离屏环境不受影响。
    """

    @staticmethod
    def flip(rect):
        """Qt 左上原点 → AppKit 左下原点（以主屏高度翻转）。"""
        import AppKit
        height = AppKit.NSScreen.screens()[0].frame().size.height
        x, y, w, h = rect
        return ((x, height - y - h), (w, h))

    def snapshot(self, window_numbers):
        """把给定窗口合成一张图（玻璃层在下、内容窗在上）；取不到返回 None。"""
        import AppKit
        import Quartz
        ids = AppKit.NSArray.arrayWithArray_([int(n) for n in window_numbers])
        image = Quartz.CGWindowListCreateImageFromArray(
            Quartz.CGRectNull, ids, Quartz.kCGWindowImageBoundsIgnoreFraming)
        if image is None or Quartz.CGImageGetWidth(image) <= 0:
            return None
        return image

    def warm(self, window_numbers):
        """在后台线程里做一次截图，把首次调用的初始化成本（实测 70~150 毫秒）提前花掉。
        后台线程没有自动释放池，手动包一个，免得每次预热漏几个小对象。"""
        import objc
        with objc.autorelease_pool():
            self.snapshot(window_numbers)

    def reduce_motion(self):
        import AppKit
        return bool(AppKit.NSWorkspace.sharedWorkspace().accessibilityDisplayShouldReduceMotion())

    def running_ids(self):
        """正在运行、会出现在 Dock 里的常规应用的 bundle id。

        用谓词 + 键值编码在 Objective-C 一侧批量取：逐个调 PyObjC 属性实测 7~10 毫秒
        （77 个进程），批量取约 1 毫秒，结果一致。"""
        import AppKit
        from Foundation import NSPredicate
        regular = "activationPolicy == %d AND bundleIdentifier != nil" % AppKit.NSApplicationActivationPolicyRegular
        apps = AppKit.NSWorkspace.sharedWorkspace().runningApplications()
        found = apps.filteredArrayUsingPredicate_(NSPredicate.predicateWithFormat_(regular))
        return [str(i) for i in found.valueForKey_("bundleIdentifier")]

    def dock_prefs(self):
        """读 Dock 偏好；返回纯数据，字段含义见 estimate 一节。"""
        from Foundation import NSUserDefaults
        domain = NSUserDefaults.standardUserDefaults().persistentDomainForName_("com.apple.dock") or {}
        units = {"file-tile": 1.0, "spacer-tile": 1.0, "small-spacer-tile": 0.5}
        pinned = []
        for entry in domain.get("persistent-apps") or []:
            data = entry.get("tile-data") or {}
            pinned.append((str(data.get("bundle-identifier") or ""), units.get(str(entry.get("tile-type")), 1.0)))
        return {
            "tile": float(domain.get("tilesize") or DOCK_DEFAULT_TILE),
            "magnified": bool(domain.get("magnification")),
            "large": float(domain.get("largesize") or 0.0),
            "recents": bool(domain.get("show-recents", True)),
            "pinned": pinned,
            "tail": len(domain.get("persistent-others") or []) + 1,
        }

    # ---- 舞台：一块点击穿透的透明窗，里面只有一层飞行的图层 ----
    def _stage_window(self, flight, level, behavior):
        import AppKit
        win = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            self.flip(flight.stage), AppKit.NSWindowStyleMaskBorderless, AppKit.NSBackingStoreBuffered, False)
        win.setReleasedWhenClosed_(False)
        win.setOpaque_(False)
        win.setHasShadow_(False)
        win.setBackgroundColor_(AppKit.NSColor.clearColor())
        win.setIgnoresMouseEvents_(True)
        win.setLevel_(level)
        win.setCollectionBehavior_(behavior)
        view = AppKit.NSView.alloc().initWithFrame_(((0, 0), (flight.stage[2], flight.stage[3])))
        view.setWantsLayer_(True)
        win.setContentView_(view)
        return win, view

    @staticmethod
    def _pose(Quartz, layer, x, y, scale, opacity):
        layer.setPosition_((x, y))
        layer.setTransform_(Quartz.CATransform3DMakeScale(scale[0], scale[1], 1.0))
        layer.setOpacity_(opacity)

    @staticmethod
    def _basic(Quartz, key, start, end, seconds, curve):
        anim = Quartz.CABasicAnimation.animationWithKeyPath_(key)
        anim.setFromValue_(start)
        anim.setToValue_(end)
        anim.setDuration_(seconds)
        anim.setTimingFunction_(Quartz.CAMediaTimingFunction.functionWithControlPoints____(*curve))
        return anim

    def open_stage(self, image, flight, level, behavior):
        """上屏舞台窗并让图层按飞行计划播放；返回 Stage。起点一帧先 flush 出去再起动画，
        这样调用方紧接着隐藏真实窗口时，舞台上已经有一模一样的画面顶着。"""
        import Quartz
        win, view = self._stage_window(flight, level, behavior)
        width, height = flight.size
        stage_h = flight.stage[3]
        (x0, y0), (x1, y1) = flight.centers
        flier = Quartz.CALayer.layer()
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setDisableActions_(True)
        flier.setBounds_(((0, 0), (width, height)))
        flier.setContents_(image)
        flier.setContentsGravity_(Quartz.kCAGravityResize)
        flier.setContentsScale_(Quartz.CGImageGetWidth(image) / float(width))
        flier.setMinificationFilter_(Quartz.kCAFilterTrilinear)
        flier.setMagnificationFilter_(Quartz.kCAFilterLinear)
        self._pose(Quartz, flier, x0, stage_h - y0, flight.scales[0], flight.fade[0][0])
        view.layer().addSublayer_(flier)
        Quartz.CATransaction.commit()
        win.orderFrontRegardless()
        Quartz.CATransaction.flush()
        self._fly(Quartz, flier, flight, stage_h)
        return Stage(win, flier)

    def _fly(self, Quartz, flier, flight, stage_h):
        """模型值先落到终点，再叠显式动画：动画播完图层就停在终点，不需要 fillMode。"""
        (x0, y0), (x1, y1) = flight.centers
        (sx0, sy0), (sx1, sy1) = flight.scales
        values, times = flight.fade
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setDisableActions_(True)
        self._pose(Quartz, flier, x1, stage_h - y1, (sx1, sy1), values[-1])
        seconds, curves = flight.duration, flight.curves
        for key, start, end, curve in (
                ("position.x", x0, x1, curves["x"]),
                ("position.y", stage_h - y0, stage_h - y1, curves["y"]),
                ("transform.scale.x", sx0, sx1, curves["sx"]),
                ("transform.scale.y", sy0, sy1, curves["sy"])):
            flier.addAnimation_forKey_(self._basic(Quartz, key, start, end, seconds, curve), "fly." + key)
        fade = Quartz.CAKeyframeAnimation.animationWithKeyPath_("opacity")
        fade.setValues_(list(values))
        fade.setKeyTimes_(list(times))
        fade.setDuration_(seconds)
        flier.addAnimation_forKey_(fade, "fly.opacity")
        Quartz.CATransaction.commit()

    def dissolve(self, stage, seconds):
        """放大落地后：舞台层重新抬到最上面（盖住刚显示的真实窗口），再淡出。"""
        import Quartz
        stage.window.orderFrontRegardless()
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setDisableActions_(True)
        stage.layer.setOpacity_(0.0)
        stage.layer.addAnimation_forKey_(
            self._basic(Quartz, "opacity", 1.0, 0.0, seconds, CURVE_SOFT), "dissolve")
        Quartz.CATransaction.commit()

    def close(self, stage):
        stage.window.orderOut_(None)
        stage.window.close()


# ---------------- 取屏幕信息的两个小入口（Qt 侧，无原生依赖） ----------------
def qt_screens():
    """[(全幅, 可用区域)]，均为 Qt 左上原点的 (x, y, w, h)。"""
    out = []
    for screen in QGuiApplication.screens():
        g, a = screen.geometry(), screen.availableGeometry()
        out.append(((g.x(), g.y(), g.width(), g.height()), (a.x(), a.y(), a.width(), a.height())))
    return out


def qt_cursor():
    pos = QCursor.pos()
    return (pos.x(), pos.y())


# ---------------- 控制器：一次只跑一段动画 ----------------
class _Run:
    """正在播的一段动画；字段全是本次动画的快照，播完即弃。"""

    def __init__(self, stage, closing, on_done):
        self.stage, self.closing, self.on_done = stage, closing, on_done
        self.phase = "flight"


class DockMotion:
    """收起 / 恢复动画的控制器。宿主主窗每个实例持有一个。

    对外：capture（收起前截图）、collapse（朝 Dock 缩小）、expand（从 Dock 放大，
    落地后才回调宿主去显示真实窗口，再溶解掉舞台）、cancel（立刻收场，不回调）、
    prewarm（后台预热首次截图）。动画本身由系统合成进程播放，这里的单次定时器只在
    飞行结束与溶解结束两个节点响；advance 是公开的，测试里直接调它推进阶段。
    """

    def __init__(self, backend=None):
        self.backend = backend if backend is not None else CocoaBackend()
        self.snapshot = None         # (图像, 窗口矩形, level, behavior)
        self.run = None
        self.timer = None
        self.prewarmed = False

    @property
    def busy(self):
        return self.run is not None

    @property
    def collapsing(self):
        return self.run is not None and self.run.closing

    @property
    def has_snapshot(self):
        return self.snapshot is not None

    def prewarm(self, window_numbers):
        """首次截图实测要 70~150 毫秒，后面只要 1 毫秒左右；放到后台线程提前花掉，
        第一次点缩小就不会"按下去没反应"。后台线程不碰窗口，只做一次截图。只做一次。"""
        if self.prewarmed:
            return None
        self.prewarmed = True
        numbers = list(window_numbers)

        def work():
            try:
                self.backend.warm(numbers)
            except Exception as exc:
                log_swallow("sf_dock_motion.prewarm", exc)

        thread = threading.Thread(target=work, name="dock-motion-prewarm", daemon=True)
        thread.start()
        return thread

    def capture(self, window_numbers, rect, level, behavior):
        """收起前调用：把玻璃层与内容窗合成一张图存着。失败只留日志，不影响收起。"""
        self.snapshot = None
        try:
            image = self.backend.snapshot(window_numbers)
        except Exception as exc:
            log_swallow("sf_dock_motion.capture", exc)
            return False
        if image is None:
            log_line("dock_motion: snapshot unavailable, collapse/expand will be instant", "WARNING")
            return False
        self.snapshot = (image, tuple(float(v) for v in rect), level, behavior)
        return True

    def icon_rect(self, window_rect, my_ids):
        """估算 Dock 里本程序图标的矩形（Qt 左上原点）；估不出返回 None。"""
        try:
            screens = qt_screens()
            if not screens:
                return None
            probed = [(dock_inset(g, a), g) for g, a in screens]
            (edge, thick), geometry = max(probed, key=lambda item: item[0][1])
            if thick <= 0:
                cx = window_rect[0] + window_rect[2] / 2.0
                cy = window_rect[1] + window_rect[3] / 2.0
                geometry = next((g for g, _a in screens if g[0] <= cx < g[0] + g[2]
                                 and g[1] <= cy < g[1] + g[3]), screens[0][0])
            prefs = self.backend.dock_prefs()
            slot = None
            if edge == "bottom" and thick > 0 and not prefs["recents"]:
                pinned_ids = {p[0] for p in prefs["pinned"]}
                extras = len([i for i in set(self.backend.running_ids())
                              if i != FINDER_ID and i not in my_ids and i not in pinned_ids])
                before, apps = locate_slot(prefs["pinned"], my_ids, extras)
                slot = (before, apps, prefs["tail"])
            rect = estimate_icon_rect(geometry, edge, thick, prefs["tile"], slot)
            if edge == "bottom" and thick > 0:
                magnified = max(prefs["large"], prefs["tile"]) if prefs["magnified"] else prefs["tile"]
                rect = refine_with_cursor(rect, qt_cursor(), geometry, thick, magnified)
            return rect
        except Exception as exc:
            log_swallow("sf_dock_motion.icon_rect", exc)
            return None

    # ---- 播放 ----
    def collapse(self, dock_rect):
        """朝 Dock 缩小。必须在真实窗口隐藏之前调用：舞台先上屏盖住，再隐藏才不闪。"""
        if self.run is not None or self.snapshot is None or dock_rect is None:
            return False
        image, rect, level, behavior = self.snapshot
        return self._start(image, plan_flight(rect, tuple(dock_rect), True), level, behavior, True, None)

    def expand(self, dock_rect, on_done):
        """从 Dock 放大回窗口原位；落地时先回调 on_done（显示真实窗口），再溶解撤舞台。"""
        if self.run is not None or self.snapshot is None or dock_rect is None:
            return False
        image, rect, level, behavior = self.snapshot
        return self._start(image, plan_flight(rect, tuple(dock_rect), False), level, behavior, False, on_done)

    def _start(self, image, flight, level, behavior, closing, on_done):
        try:
            if self.backend.reduce_motion():
                return False
            stage = self.backend.open_stage(image, flight, level, behavior)
        except Exception as exc:
            log_swallow("sf_dock_motion.start", exc)
            return False
        self.run = _Run(stage, closing, on_done)
        self._arm(flight.duration + FINISH_SLACK)
        return True

    def _arm(self, seconds):
        if self.timer is None:
            self.timer = QTimer()
            self.timer.setSingleShot(True)
            self.timer.setTimerType(Qt.TimerType.PreciseTimer)
            self.timer.timeout.connect(self.advance)
        self.timer.start(int(round(seconds * 1000)))

    def advance(self):
        """推进阶段：飞行结束 → 放大则显示真实窗口并溶解，缩小则直接收场；溶解结束 → 收场。"""
        run = self.run
        if run is None:
            return
        if run.closing or run.phase != "flight":
            self._finish()
            return
        run.phase = "dissolve"
        stage = run.stage
        try:
            if run.on_done is not None:
                run.on_done()
        except Exception as exc:
            log_swallow("sf_dock_motion.on_done", exc)
            self._finish()
            return
        if self.run is not run:
            return          # 回调里又触发了取消（比如马上再收起），舞台已撤，不能再溶解
        try:
            self.backend.dissolve(stage, DISSOLVE_SEC)
        except Exception as exc:
            log_swallow("sf_dock_motion.dissolve", exc)
            self._finish()
            return
        self._arm(DISSOLVE_SEC + FINISH_SLACK)

    def _finish(self):
        """收场：先把状态摘干净再撤舞台；放大播完才释放截图，缩小要留着给恢复用。"""
        run, self.run = self.run, None
        if self.timer is not None:
            self.timer.stop()
        if run is None:
            return
        if not run.closing:
            self.snapshot = None
        self._drop(run.stage)

    def _drop(self, stage):
        try:
            self.backend.close(stage)
        except Exception as exc:
            log_swallow("sf_dock_motion.close_stage", exc)

    def cancel(self):
        """立刻收场，不回调。用于动画中途又来了相反的操作，或退出。"""
        run, self.run = self.run, None
        if self.timer is not None:
            self.timer.stop()
        if run is not None:
            self._drop(run.stage)
