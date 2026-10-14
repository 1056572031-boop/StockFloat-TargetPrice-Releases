# -*- coding: utf-8 -*-
"""玻璃背景层的真链路子进程替身（被 tests/test_coverage_glass_setup_chain.py 调起）。

四个模式：probe（有没有窗口服务）、setup（正常建层）、setup_fail（已上屏后失败的收口）、
force_top（置顶复核：稳态不写、倒序自愈）。

为什么单独一个子进程：`_setup_glass` 是本仓唯一**必须在屏幕上建 NSWindow** 的入口，
而"同进程里建 AppKit 窗口会把整个测试会话冲掉"在这里已经付过学费（见
`tests/_alert_child.py` 同样的取舍）。它又必须真跑一遍 —— 桩掉 AppKit 只能证明
"我们把什么参数递给了 AppKit"，证明不了"用户屏幕上有没有一块点击穿透的毛玻璃"。

为什么不闪现在你屏幕上：宿主只 `winId()`（造出平台窗口但不映射上屏），随后把内容窗与
背景窗都 `setAlphaValue_(0.0)` —— 像素消失，几何与合成顺序两问照原样可查。
一开始用的是"把原点钉到 (-12000,-12000)"，实测**不行**：Qt 在 orderFront 时会按自己的
模型把原点改回来（窗口照样出现在桌面上），而真的钉出屏幕之后，`OnScreenOnly` 列表里
两窗 bounds 差出 1.2 万 pt，`_steady_state_fault` 永远判"背景层错位"、合成顺序那条也
测不真 —— 夹具把判据污染了。

只跑 `sf_glass` 这一层，不建 FloatWindow：主窗会把行情、渲染、更新整条栈都拖进来，
与本条判据无关，还会让子进程的启动时间变成判据的一部分。
"""

import os
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 只有 cocoa 下 `winId()` 才是 NSView*。离屏下把它当指针是**段错误**而不是异常，
# 所以这条必须由子进程自己钉死，不能靠父进程传进来的 QT_QPA_PLATFORM。
os.environ["QT_QPA_PLATFORM"] = "cocoa"
_TMP = pathlib.Path(os.environ.get("TMPDIR", "/tmp"))
os.environ.setdefault("SF_LOG_PATH", str(_TMP / "sf_glass_child.log"))
os.environ.setdefault("SF_CONFIG_PATH", str(_TMP / "sf_glass_child_config.json"))
os.environ.setdefault("SF_UNIVERSE_PATH", str(_TMP / "sf_glass_child_securities.json"))
os.environ.setdefault("SF_SINGLE_SOCK", str(_TMP / "sf_glass_child.sock"))

from PyQt6.QtWidgets import QApplication, QWidget    # noqa: E402

import AppKit                                        # noqa: E402

import sf_glass                                      # noqa: E402
from sf_glass import GlassMixin                      # noqa: E402

MAIN_W, MAIN_H = 184, 224

_APP = None


def _marker(key, value):
    print("%s=%s" % (key, value), flush=True)


def _app():
    """QApplication 必须被**模块级引用**持有。

    实测踩到的：写成 `_app()` 不接返回值，那个临时对象在语句结束就被回收，之后
    `platformName()` 不再返回 cocoa —— 预检会报 not_cocoa，看起来像"这台机器没有
    窗口服务"，实际是我们自己把 app 丢了（同一条坑 tests/test_v1443 也记着）。
    """
    global _APP
    if _APP is None:
        _APP = QApplication.instance() or QApplication([])
    return _APP


class Host(GlassMixin, QWidget):
    """最小宿主：只要 `_setup_glass` 真正会碰的那几样。

    状态初始化走产品自己的 `_init_glass_state()`，不在这里手抄一遍属性 ——
    手抄会把"漏写一个属性"这类缺陷在子进程里也掩盖掉。
    """

    # `_force_top` 末尾会抬"已打开的工具窗"。那是主窗（FloatWindow）的职责，
    # 与背景层层级判据无关；不给 no-op 的话整段会被 broad except 咽成一行
    # SWALLOWED，置顶路径的后半截就根本没跑（实测第一版就是这样）。
    _sync_open_tool_windows = lambda self: None       # noqa: E731
    _raise_open_tool_windows = lambda self: None      # noqa: E731

    def __init__(self):
        QWidget.__init__(self)
        self._init_glass_state()
        self.retop = []

    def _log_retop(self, reason, detail=""):
        """取证日志的收口点：本 harness 判"看门狗到底发现了几次、发现的是什么"。"""
        self.retop.append(reason + detail)


_REAL_NSWINDOW = AppKit.NSWindow    # 先抓住真类：替换之后再也取不到，会自己套自己递归


class _WindowFactory:
    """只替 `_setup_glass` 里那一句 `AppKit.NSWindow.alloc()` 记账。

    为什么不直接 subclass NSWindow：PyObjC 里 ObjC 子类必须用 `objc.super`，
    实测用 Python 的 `super()` 会在 init 阶段就抛 AttributeError，`_setup_glass`
    返回 False —— 那等于把"被测路径"换成了"我们的胶水路径"，测出来的红绿都不算数。
    这里保持真 NSWindow 不变，只在外面套一层转发：拿到的就是产品真正建出来的那块窗。
    """

    def __init__(self):
        self.made = []

    def alloc(self):
        outer = self
        real = _REAL_NSWINDOW.alloc()

        class _Recorded:
            def initWithContentRect_styleMask_backing_defer_(self, *args):
                out = real.initWithContentRect_styleMask_backing_defer_(*args)
                outer.made.append(real)
                return out

            def __getattr__(self, name):
                return getattr(real, name)

        return _Recorded()


def _quiet_host():
    """造一个「平台窗口已存在、但屏幕上看不见」的宿主：winId() 不 show + alpha 0。"""
    app = _app()
    host = Host()
    host.resize(MAIN_W, MAIN_H)
    host.winId()                       # 只造 NSWindow，不映射上屏
    app.processEvents()
    host.move(24, 24)          # 留在屏内：几何与合成顺序才测得准；像素靠 alpha 遮掉
    nsw = sf_glass.content_ns_window(host)
    _hide(nsw)
    return host


def _hide(*windows):
    """把窗口调成**全透明**：屏幕上看不见，但在窗口列表里仍然完全有效。

    为什么不用"挪到屏幕外"这一招（我第一版就是这么做的，实测作废）：
    Qt 会在窗口被 orderFront 时按自己的模型重新摆放原点，`setFrameOrigin_` 拦不住；
    而真把两窗钉到 (-12000,-12000) 之后，`OnScreenOnly` 那份列表里的 bounds 差出
    1.2 万 pt，`_steady_state_fault` 一直判"背景层错位"，合成顺序那条也测不真 ——
    **判据被夹具自己污染了**。`setAlphaValue_(0.0)` 只让像素消失，几何与顺序
    两问都照原样可查（本机实测 alpha=0 时 `glass_main_real_state` 仍返回完整结果）。
    """
    for w in windows:
        if w is None:
            continue
        try:
            w.setAlphaValue_(0.0)
        except Exception:
            pass


def _composite_state(main_number, glass_number):
    """问产品自己的那一句"用户此刻实际看到什么"，而不是在这里另造一套判据。

    为什么不用 `kCGWindowListOptionAll` 的下标判先后（一开始我是这么写的，实测作废）：
    同一层级（这里两窗都是 level 0）在 All 列表里是**按窗口号**排的，与合成先后无关 ——
    本机实测 All 给 main=5 / glass=4（看着像玻璃在上），而 OnScreenOnly 列表
    （`glass_main_real_state` 用的那一份）给的是内容窗在前。判据用错一份列表，
    就会把"顺序是对的"报成"玻璃压住了内容"，反过来也会把真倒序放过。
    """
    return sf_glass.glass_main_real_state(main_number, glass_number)


def run_probe():
    """预检：这台机器有没有可用的窗口服务。没有就整体跳过，不假装通过。"""
    app = _app()
    if not sf_glass.native_cocoa_active():
        _marker("probe", "not_cocoa(platform=%s)" % app.platformName())
        return 1
    host = _quiet_host()
    nsw = sf_glass.content_ns_window(host)
    if nsw is None:
        _marker("probe", "no_nswindow")
        return 1
    _marker("probe", "ok")
    _marker("probe_frame", "%dx%d" % (round(nsw.frame().size.width),
                                      round(nsw.frame().size.height)))
    return 0


def run_setup():
    """正常路径：玻璃层建出来、在主窗之下、不拦鼠标、两边都没阴影。"""
    factory = _WindowFactory()
    real_nswindow = AppKit.NSWindow
    AppKit.NSWindow = factory
    try:
        host = _quiet_host()
        ok = host._setup_glass()           # discipline-ok(R5): 被测对象本身就是 GlassMixin 的私有入口
        _app().processEvents()           # 让 WindowServer 真的收下这一对窗口
        nsw = sf_glass.content_ns_window(host)
        gw = getattr(host, "_glass_window", None)
        _hide(gw, nsw)
        _marker("setup", bool(ok))
        _marker("setup_return_matches_state", bool(ok) == (gw is not None))
        _marker("glass_created", len(factory.made))
        _marker("visible", bool(gw is not None and gw.isVisible()))
        _marker("same_level", bool(gw is not None and nsw is not None
                                   and gw.level() == nsw.level()))
        _marker("ignores_mouse", bool(gw is not None and gw.ignoresMouseEvents()))
        _marker("shadow_off_main", bool(nsw is not None and not nsw.hasShadow()))
        _marker("shadow_off_glass", bool(gw is not None and not gw.hasShadow()))
        _marker("mask_attached", getattr(host, "_glass_mask_layer", None) is not None)
        _marker("material", getattr(host, "_glass_view", None).material()
                if getattr(host, "_glass_view", None) is not None else -1)
        _marker("glass_same_size", bool(gw is not None and nsw is not None
                                        and gw.frame().size.width == nsw.frame().size.width
                                        and gw.frame().size.height == nsw.frame().size.height))
        main_num = int(nsw.windowNumber()) if nsw is not None else -1
        glass_num = int(gw.windowNumber()) if gw is not None else -1
        st = _composite_state(main_num, glass_num)
        _marker("watchdog_sees_this_pair", st is not None)
        _marker("glass_on_top", "unknown" if st is None else st["glass_on_top"])
        host._close_glass()                # discipline-ok(R5): 收摊这条路径本身就是被测对象
        # 子窗关闭只能 orderOut：close 会让 AppKit 与 PyObjC 抢所有权，
        # 自动释放池 drain 时崩（v1.2.90 的现场崩溃）。判据就落在"此刻不可见"。
        _marker("hidden_after_close", bool(gw is not None and not gw.isVisible()))
    finally:
        AppKit.NSWindow = real_nswindow
    return 0


def run_setup_fail():
    """玻璃层已经上屏、第二次圆角 mask 抛异常：失败收口必须把它收掉。

    为什么挑"第二次"：`_setup_glass` 里 `orderFront_` 在两次 `_set_glass_mask` 中间，
    第一次就失败的话那块玻璃根本还没上屏，cleanup 分支（orderOut + 把主窗阴影补回来）
    走不到，也就测不到"桌面上留下一块点击穿透的毛玻璃"这个真实故障。
    """
    factory = _WindowFactory()
    real_nswindow = AppKit.NSWindow
    AppKit.NSWindow = factory
    try:
        host = _quiet_host()
        hits = []

        def mask(*_args):
            hits.append(1)
            if len(hits) >= 2:
                raise RuntimeError("mask rasterize failed")

        host._set_glass_mask = mask        # discipline-ok(R5): 注入失败点必须挂在这个私有步骤上，才有「已上屏之后才炸」那一拍
        ok = host._setup_glass()           # discipline-ok(R5): 同上
        _app().processEvents()
        nsw = sf_glass.content_ns_window(host)
        orphan = factory.made[-1] if factory.made else None
        _hide(nsw, orphan)
        _marker("setup", bool(ok))
        _marker("mask_attempts", len(hits))
        _marker("glass_created", len(factory.made))
        _marker("state_reset",
                getattr(host, "_glass_window", 0) is None
                and getattr(host, "_glass_view", 0) is None
                and getattr(host, "_glass_mask_layer", 0) is None)
        # 主窗阴影：309 那行无条件关掉过，失败时必须补回来，否则主窗一直"没有边"
        _marker("shadow_back", bool(nsw is not None and nsw.hasShadow()))
        # 这块玻璃此刻必须不在屏上，否则桌面上永久多一块点击穿透层（v1.4.18 走查补的收口）
        _marker("orphan_not_visible", bool(orphan is not None and not orphan.isVisible()))
    finally:
        AppKit.NSWindow = real_nswindow
    return 0


def _wait_order(numbers, want_glass_on_top, rounds=40, step=0.05):
    """反复读 OnScreenOnly 那份列表，直到合成顺序变成要的样子。

    这里睡的是**系统反映时间**，不是"被测行为需要多久"：`orderFront` /
    `orderWindow:relativeTo:` 发出去之后，WindowServer 把新顺序写回列表是异步的，
    满载时那一拍读到的是旧顺序 —— 本机随机序全量轮两次都栽在这里（一次读到"没倒"，
    一次读到"没修好"），看起来像产品坏了，其实是判据在读一个还没更新的缓存。
    生产的看门狗本来就是 250ms 一拍一直重试，所以"多读几拍"与真实节奏一致。
    返回 (是否达成, 用了几轮)；轮次跑满仍未达成就是真失败，调用方必须把它判红。
    """
    for i in range(1, rounds + 1):
        _app().processEvents()
        st = _composite_state(*numbers)
        if st is not None and bool(st["glass_on_top"]) == want_glass_on_top:
            return True, i
        time.sleep(step)
    return False, rounds


def run_force_top():
    """置顶复核的真路径：稳态必须收敛到"什么都不写"，倒序必须能自己纠正。

    为什么值得单独跑一次真窗口：`_force_top` 里那十几行（含 v1.4.35 收尾的
    `orderWindow:relativeTo:`）在离屏用例里永远走不到 —— 离屏拿不到内容窗，
    整条第一行就 return。而这一段守的正是用户报的"只剩背景会不会永久"。

    ①~② 是 v1.4.10 的省电不变量：体检通过时一次系统写都不发。
    ③~⑤ 是 v1.4.26/33/35 的自愈不变量：顺序倒了要判得出、纠得回、之后回到稳态。
    """
    host = _quiet_host()
    if not host._setup_glass():  # discipline-ok(R5): 被测对象本身就是 GlassMixin 的私有入口
        _marker("error", "glass not built")
        return 2
    nsw = sf_glass.content_ns_window(host)
    gw = getattr(host, "_glass_window", None)
    _hide(gw, nsw)
    _app().processEvents()
    numbers = (int(nsw.windowNumber()), int(gw.windowNumber()))

    # ① 第一拍：层级还没到不变量上，必须真的做一次完整置顶
    host._force_top()  # discipline-ok(R5): 被测对象本身就是 GlassMixin 的私有入口
    _app().processEvents()
    _marker("level_after_first_pass", int(nsw.level()))
    _marker("glass_level_follows", bool(gw.level() == nsw.level()))
    _marker("fixes_first_pass", int(getattr(host, "_top_fixes", 0)))

    # ② 稳态收敛：连续几拍之后，"完整置顶"的次数必须不再增长
    fixes_after_first = int(getattr(host, "_top_fixes", 0))
    converged = False
    for _ in range(5):
        _app().processEvents()
        host._force_top()  # discipline-ok(R5): 被测对象本身就是 GlassMixin 的私有入口
        now_fixes = int(getattr(host, "_top_fixes", 0))
        if now_fixes == fixes_after_first:
            converged = True
            break
        # 没收敛：再补一拍，看是不是"服务端位置还没跟上"这种一次性的滞后
        fixes_after_first = now_fixes
    _marker("steady_state_converged", converged)
    _marker("steady_state_logs_nothing", not host.retop)
    _marker("fixes_at_steady", fixes_after_first)

    # ③ 注入真实故障：把玻璃层 orderFront 到内容窗之上
    #    （与 tools/glass_occlusion_probe.py 同一手法），先证明它真的倒了
    gw.orderFront_(None)
    inverted_ok, inverted_rounds = _wait_order(numbers, True)
    _marker("probe_sees_pair_before_fix",
            inverted_ok or (_composite_state(*numbers) is not None))
    _marker("inverted_before_fix", inverted_ok)
    _marker("probe_rounds_to_see_inversion", inverted_rounds)

    # ④ 下一拍到期（把节流标记当"到期"处理，不靠 sleep 猜时长）。
    #    这里**允许连续几拍**：生产里看门狗本来就是 250ms 一拍一直重试，
    #    而 `orderWindow:relativeTo:` 的效果要等 WindowServer 把新顺序反映到
    #    OnScreenOnly 列表里 —— 只拍一次就去读结论，在满载的机器上会偶发读到旧顺序
    #    （本机随机序全量轮实测第 1 号红就是这么来的）。判据改成"≤5 拍内必须自愈"，
    #    既守住"只剩背景是瞬态"这条承诺，也不把一次系统往返的时序当不变量。
    healed_at = 0
    after_ok = False
    for attempt in range(1, 6):
        host._last_z_order_check = 0.0  # discipline-ok(R5): 把节流标记拨到到期，等价于下一拍；不靠 sleep 猜时长（AGENTS §6 第 3 条）
        host._force_top()  # discipline-ok(R5): 被测对象本身就是 GlassMixin 的私有入口
        after_ok, _rounds = _wait_order(numbers, False, rounds=20)
        if after_ok:
            healed_at = attempt
            break
    _marker("probe_sees_pair_after_fix", after_ok or
            (_composite_state(*numbers) is not None))
    # 判"修好了"必须建立在"探针这次真的看见了这一对"之上：拿空结果当已修复，
    # 就成了一条永远不会失败的检查（AGENTS §3.1 那条"一个永不失败的检查等于没检查"）。
    _marker("healed", healed_at > 0)
    _marker("passes_to_heal", healed_at)
    _marker("retop_reason", ",".join(host.retop))
    _marker("fixes_after_fault",
            int(getattr(host, "_top_fixes", 0)) > fixes_after_first)

    # ⑤ 纠正之后必须回到"什么都不写"的稳态（判收敛，不判某一拍的计数）
    settled = False
    for _ in range(3):
        baseline = int(getattr(host, "_top_fixes", 0))
        host._last_z_order_check = 0.0  # discipline-ok(R5): 同上
        host._force_top()  # discipline-ok(R5): 同上
        _app().processEvents()
        if int(getattr(host, "_top_fixes", 0)) == baseline:
            settled = True
            break
    _marker("settles_back_to_steady", settled)
    return 0


MODES = {"probe": run_probe, "setup": run_setup, "setup_fail": run_setup_fail,
         "force_top": run_force_top}


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    handler = MODES.get(mode)
    if handler is None:
        print("usage: _glass_child.py probe|setup|setup_fail|force_top", flush=True)
        sys.exit(3)
    try:
        sys.exit(handler())
    except Exception as exc:                        # noqa: BLE001
        _marker("error", repr(exc))
        sys.exit(2)
