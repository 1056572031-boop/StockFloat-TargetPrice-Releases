# -*- coding: utf-8 -*-
"""v1.4.26 回归：玻璃背景永不得盖住主窗内容。

现场（用户 2026-09-23 报告）：浏览器全屏下主界面"只剩一块深色圆角背景，行情/
分时图/顶部按钮全都不见，但按原位置盲点关闭按钮仍然能退出程序"。

**先说清楚哪些是实测成立的、哪些不是**（v1.4.27 复核 F1 改写本段）：
  · 触发点**没有找到**。上一轮把它写成"缩小到 Dock → 恢复，本机 100% 复现"，
    本轮 A/B（关掉下述 ①③ 两层 = 完整还原 v1.4.25 行为）走真实的
    minimize_to_dock / _restore_window，恢复后 1.2 秒与再等 6 秒两次采样都是
    主窗在上，未复现；真 Chrome 三次进出系统全屏同样全程主窗在上。
  · 失效机制**实测成立**：外部一次 `gw.orderFront_` 造成倒序后，v1.4.25 行为
    永不自愈，v1.4.26 下 3.45 秒内被自动纠正。
所以本文件守的是"倒序不可能再被永久固化"，不是"用户那条操作路径已经修好了"。

失效机制：主窗与玻璃窗同为 NSStatusWindowLevel(25)，先后只由最后一次 orderFront
决定。`_restore_window` 这类入口末尾会把玻璃窗 `orderFront_` 提到最前，紧接着的
`_force_top()` 又被 v1.4.10 的稳态体检 `_top_state_ok` 判为"状态已正确"直接
return —— 体检只比对层级/可见/frame/Space，**看不出两窗谁在上**（玻璃层
ignoresMouseEvents，点击穿透到下面的主窗，所以按钮仍"有效"）。于是"玻璃在上、
内容在下"被永久固化，250ms 一拍的复核全部空转。

修法（三层）：
  ① 刚把玻璃层 orderFront 过、之后不补主窗的调用方，必须重做一次完整置顶
     （v1.4.27 起经不带形参的 `_reassert_top()` 跳过稳态体检）
     —— 挡住我们已知的这一个入口。
  ② 活动 Space 变化（第三方进出系统全屏、用户切桌面）即刻重排，响应 ≤250ms。
  ③ 稳态体检之外再加一道低频的**合成顺序**复核（`glass_covers_main`，5 秒一次）
     —— 倒序若由别的原因发生（系统重排、未来新增的上屏路径），窗口属性读起来
     全是"对的"，只有 WindowServer 的实际前后顺序能揭穿它。
层级关系与外观一律不动 —— 实测拉开层级或改用 child window 都会在第三方全屏
Space 里造成更严重的问题，详见 sf_glass._TOP_WINDOW_LEVEL 注释。

断言口径：①②③ 只认"发给原生窗口的调用序列""查 WindowServer 时用了哪两个窗口
号、查了几次、真函数拿到窗口列表怎么判序"这类可观测输出，不拿真机窗口状态赌用例。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sf_glass
from tests.test_power_v1410 import _FakeNSWindow, _FakeRect, _WinCase


def _ok_pair():
    """一对"状态完全正确"的替身窗口（主窗 + 玻璃窗，同为 25 级、同 frame）。"""
    behavior = sf_glass.public_overlay_collection_behavior()
    frame = _FakeRect("same")
    main = _FakeNSWindow(level=25, behavior=behavior, frame=frame)
    glass = _FakeNSWindow(level=25, behavior=behavior, frame=frame)
    return main, glass


def _ws(on_top=False, desync=0.0):
    """伪造 `glass_main_real_state()` 的返回：WindowServer 眼里的顺序 + 真实错位。

    v1.4.33 起这两问共用一次查询，所以本类原先打桩 `glass_covers_main` 的位点
    一律改打这个新入口（"要骗的是 WindowServer 的回答"，不是"要骗哪个函数"）。
    """
    return mock.Mock(return_value={"glass_on_top": on_top, "desync_px": desync})


def _patch_objc(case, win, main):
    """把 _force_top 里的 objc 取窗链路换成替身窗口（离屏没有真 NSWindow）。"""
    view = mock.Mock()
    view.window.return_value = main
    fake_objc = mock.Mock()
    fake_objc.objc_object.return_value = view
    case.track_patch(mock.patch.object(sf_glass, "_objc_deps",
                                       return_value=(fake_objc, mock.Mock())))
    # v1.4.33：取 NSWindow 的链路统一走 content_ns_window()，它第一行就问平台
    # （offscreen 下 winId() 不是 NSView*，真去转换是**不可捕获的段错误**）。
    # 本用例要驱动的是"拿到窗口之后"的真实逻辑，所以显式声明"假装在 cocoa 上"。
    case.track_patch(mock.patch.object(sf_glass, "native_cocoa_active",
                                       lambda: True))
    case.track_patch(mock.patch.object(win, "winId", return_value=1))
    case.track_patch(mock.patch.object(win, "_sync_glass_frame",
                                       lambda *a, **k: None))
    case.track_patch(mock.patch.object(win, "_sync_open_tool_windows",
                                       lambda: None))


class _NumberedWindow(_FakeNSWindow):
    """带固定窗口号的替身：合成顺序复核要拿窗口号去查 WindowServer。"""

    def __init__(self, number, **kw):
        _FakeNSWindow.__init__(self, **kw)
        self._number = number

    def windowNumber(self):
        return self._number


class _FakeMonotonic(object):
    """可控时钟：sf_glass 只用 time.monotonic()，这里整块换掉，
    免得用例去赌"现在几点/跑了多久"（AGENTS.md 第 6 节第 4 条）。"""

    def __init__(self, start=1000.0):
        self.now = start

    def monotonic(self):
        return self.now

    def advance(self, secs):
        self.now += secs


class TestForceTopHonoursForce(_WinCase):
    """`_reassert_top()` 必须重做置顶；常规复核的稳态零写入不能回退。

    v1.4.26 用 `_force_top(force=True)` 表达"跳过体检"，v1.4.27 复核 F2 把它换成
    不带形参的 `_reassert_top()`：全仓替身按 `lambda self: None` 打桩，传参就撞
    TypeError。两类断言都只认"发给原生窗口的调用序列"，不认形参本身。
    """

    def test_reassert_top_redoes_top_even_when_state_is_ok(self):
        import stock_float_window as m
        win = self.make_win()
        main, glass = _ok_pair()
        win._glass_window = glass
        self._patch_objc(win, main)
        self.track_patch(mock.patch.object(
            m.FloatWindow, "_force_top", sf_glass.GlassMixin._force_top))
        sf_glass.GlassMixin._reassert_top(win)
        self.assertIn(("orderFrontRegardless", None), main.writes,
                      "强制置顶仍被稳态体检跳过：刚上屏的玻璃层会继续盖住内容")
        self.assertFalse(win._force_top_forced,
                         "开关没复位：之后每一拍都会退化成全量置顶")

    def test_without_force_steady_state_still_writes_nothing(self):
        import stock_float_window as m
        win = self.make_win()
        main, glass = _ok_pair()
        win._glass_window = glass
        self._patch_objc(win, main)
        self.track_patch(mock.patch.object(
            m.FloatWindow, "_force_top", sf_glass.GlassMixin._force_top))
        sf_glass.GlassMixin._force_top(win)
        self.assertEqual(main.writes, [], "稳态复核退化成逐拍全量置顶（v1.4.10 回退）")
        self.assertEqual(glass.writes, [])

    def _patch_objc(self, win, main):
        _patch_objc(self, win, main)


class TestDockRestoreKeepsContentOnTop(_WinCase):
    """缩小到 Dock → 恢复：玻璃层上屏之后，主窗必须被重新提到它之上。"""

    def _prep(self):
        import stock_float_window as m
        win = self.make_win()
        # 夹具把常驻循环停了（running=False），恢复路径据此拒绝执行，这里还原真实态
        win.running = True
        win._minimized = True
        _, glass = _ok_pair()
        win._glass_window = glass
        seq = []
        glass.writes = seq
        self.track_patch(mock.patch.object(
            m.FloatWindow, "_reassert_top", lambda self: seq.append(("reassert_top",))))
        for name in ("show", "_sync_dot_timer", "_force_fetch", "_poll",
                     "_update_nap_policy", "_is_trading_time"):
            self.track_patch(mock.patch.object(win, name, lambda *a, **k: None))
        for name in ("timer", "top_timer"):
            self.track_patch(mock.patch.object(win, name, mock.Mock()))
        return win, seq

    def test_restore_reasserts_main_after_ordering_glass_front(self):
        """顺序必须是"玻璃上屏 → 强制重置顶"；少了后者就是用户看到的空壳。"""
        win, seq = self._prep()
        win._restore_window()
        self.assertEqual(
            seq, [("orderFront_", None), ("reassert_top",)],
            "恢复窗口后没有强制重新置顶：玻璃层留在主窗之上，内容被永久盖住")

    def test_restore_still_brings_background_back(self):
        """玻璃层本身也要重新上屏，否则恢复后只剩内容、没有深色底。"""
        win, seq = self._prep()
        win._restore_window()
        self.assertIn(("orderFront_", None), seq)

    def test_restore_window_rejects_when_not_minimized(self):
        """防重复恢复：未处于缩 Dock 状态时不得动窗口（旧行为不回退）。"""
        win, seq = self._prep()
        win._minimized = False
        win._restore_window()
        self.assertEqual(seq, [], "非最小化态被恢复流程重新拉起，会与退出/缩小争抢生命周期")


class TestStaleOrderSelfHeal(_WinCase):
    """稳态体检看不出"谁压住谁"，必须由低频的合成顺序复核兜底。

    为什么需要这一层：① 的强制重置顶只挡住了"我们自己刚把玻璃层 orderFront 过"这一
    个已知入口。倒序一旦由别的原因发生（系统在我们两窗之间的重排、未来新增的
    上屏路径），_top_state_ok 读到的窗口属性**全都是对的**，250ms 一拍的复核会
    永久空转 —— 用户看到的就是一块再也点不动内容的深色背景。
    """

    def _win_with_pair(self):
        import stock_float_window as m
        win = self.make_win()
        behavior = sf_glass.public_overlay_collection_behavior()
        frame = _FakeRect("same")
        main = _NumberedWindow(7001, level=25, behavior=behavior, frame=frame)
        glass = _NumberedWindow(7002, level=25, behavior=behavior, frame=frame)
        win._glass_window = glass
        _patch_objc(self, win, main)
        self.track_patch(mock.patch.object(
            m.FloatWindow, "_force_top", sf_glass.GlassMixin._force_top))
        return win, main, glass

    def test_inverted_order_retriggers_full_top(self):
        """窗口属性全对、但合成顺序倒了 → 必须走完整置顶把顺序纠正回来。"""
        win, main, _ = self._win_with_pair()
        self.track_patch(mock.patch.object(sf_glass, "glass_main_real_state",
                                           _ws(on_top=True)))
        sf_glass.GlassMixin._force_top(win)
        self.assertIn(("orderFrontRegardless", None), main.writes,
                      "玻璃层压在主窗之上却没被纠正：就是用户看到的空壳")

    def test_order_probe_is_throttled(self):
        """合成顺序复核按 _Z_ORDER_CHECK_INTERVAL 节流，不能退化成每拍一次
        WindowServer 往返（那是 v1.4.10 省电结论的回退）。

        次数**按常量算**，不写死：v1.4.33 把间隔从 5s 降到 1s（倒序后用户盯空白的
        时间从 4.46s 降到 0.39s，见 sf_glass 里的实测注释），写死"10 秒 2 次"的
        用例会在那次改动里红成"节流坏了"，而它其实只是常量变了 —— 守的应该是
        "按这个间隔节流"，不是"恰好 2 次"。
        """
        win, main, _ = self._win_with_pair()
        probe = _ws()
        clock = _FakeMonotonic()
        self.track_patch(mock.patch.object(sf_glass, "glass_main_real_state", probe))
        self.track_patch(mock.patch.object(sf_glass, "time", clock))
        ticks = 40                                   # 40 拍 × 250ms = 10 秒
        for _ in range(ticks):
            sf_glass.GlassMixin._force_top(win)
            clock.advance(0.25)
        # 起点查一次，之后每满一个节流窗再查一次；最后一拍**先查后走时**，
        # 所以覆盖的时间跨度是 (ticks-1)*0.25 而不是 ticks*0.25。
        span_s = (ticks - 1) * 0.25
        expected = int(span_s // sf_glass._Z_ORDER_CHECK_INTERVAL) + 1  # discipline-ok(R5): 期望值必须由这个常量推导，写死数字才是本末倒置
        self.assertEqual(probe.call_count, expected,
                         "10 秒内查了 %d 次，按 %ss 节流应为 %d 次" % (
                             probe.call_count, sf_glass._Z_ORDER_CHECK_INTERVAL,
                             expected))
        self.assertLess(probe.call_count, ticks,
                        "查的次数等于拍数 = 节流已失效，退化成每拍一次往返")
        self.assertEqual(main.writes, [], "顺序没倒却每拍全量置顶")

    def test_probe_asks_windowserver_with_both_window_numbers(self):
        """查的是"这一对窗口的真实窗口号"，不是别的窗口。"""
        win, main, glass = self._win_with_pair()
        probe = _ws()
        self.track_patch(mock.patch.object(sf_glass, "glass_main_real_state", probe))
        sf_glass.GlassMixin._force_top(win)
        probe.assert_called_once_with(7001, 7002)

    def test_unknown_order_does_not_churn(self):
        """查不到顺序（窗口未上屏等）时返回 False，不得因此每拍全量置顶。"""
        win, main, _ = self._win_with_pair()
        clock = _FakeMonotonic()
        self.track_patch(mock.patch.object(sf_glass, "time", clock))
        self.track_patch(mock.patch.object(sf_glass, "glass_main_real_state", _ws()))
        sf_glass.GlassMixin._force_top(win)
        clock.advance(sf_glass._Z_ORDER_CHECK_INTERVAL + 0.1)   # 下一轮到点
        sf_glass.GlassMixin._force_top(win)
        self.assertEqual(main.writes, [])


class TestActiveSpaceChangeRetops(_WinCase):
    """第三方应用进出系统全屏 = 用户报告的现场，必须**下一拍内**重排，不能等 5 秒。

    为什么单靠合成顺序复核不够：Space 切换由 WindowServer 重排本进程的两个窗，
    倒序可能在没有任何产品调用的情况下发生；5 秒节流意味着用户要盯着一块空壳
    背景最多 5 秒。活动 Space 的 id 变化就是"系统刚替我们重排过"的现成信号，
    单次读取实测 0.76µs，比一次 CGWindowList 查询便宜三个数量级。
    """

    def _win(self, space_ids):
        import stock_float_window as m
        win = self.make_win()
        main, glass = _ok_pair()
        win._glass_window = glass
        _patch_objc(self, win, main)
        self.track_patch(mock.patch.object(sf_glass, "active_space_id",
                                           side_effect=list(space_ids)))
        # 合成顺序复核整体关掉：本类只守"Space 变了就重排"这一条，
        # 两层各守各的，别互相兜底把断言糊成一团。
        self.track_patch(mock.patch.object(sf_glass, "glass_main_real_state", _ws()))
        self.track_patch(mock.patch.object(
            m.FloatWindow, "_force_top", sf_glass.GlassMixin._force_top))
        return win, main

    def test_space_change_retriggers_full_top(self):
        """窗口属性全对、合成顺序也还没到点查，但 Space 换了 → 必须重排一次。"""
        win, main = self._win([1, 1, 7, 7])
        sf_glass.GlassMixin._force_top(win)          # 记下"置顶是在 space 1 做的"
        sf_glass.GlassMixin._force_top(win)          # 同一 Space：稳态，零写入
        self.assertEqual(main.writes, [], "同一 Space 内退化成逐拍全量置顶")
        sf_glass.GlassMixin._force_top(win)          # Space 1 -> 7：Chrome 进全屏
        self.assertIn(("orderFrontRegardless", None), main.writes,
                      "活动 Space 变了却没重新置顶：全屏里就是用户看到的空壳")

    def test_space_change_arms_next_order_probe(self):
        """检测到 Space 变化时把合成顺序复核的节流清零，下一拍就真查一次。

        重排可能发生在系统切换动画**之后**，只补一次置顶不够，必须再看一眼。
        """
        win, _ = self._win([1, 1, 7])
        sf_glass.GlassMixin._force_top(win)
        sf_glass.GlassMixin._force_top(win)
        win._last_z_order_check = 12345.0            # 假装"刚查过"
        self.assertTrue(win._active_space_changed())
        self.assertEqual(win._last_z_order_check, 0.0,
                         "Space 变了却没让下一拍复查合成顺序")

    def test_unreadable_space_does_not_churn(self):
        """读不到 Space id（非 macOS / CGS 不可用）时一律判"没变"，
        不得因此退化成每拍全量置顶。"""
        win, main = self._win([None] * 40)
        for _ in range(40):
            sf_glass.GlassMixin._force_top(win)
        self.assertEqual(main.writes, [])


class TestGlassCoversMainProbe(unittest.TestCase):
    """③ 层真函数的判序本身（v1.4.27 复核 F3 补）。

    为什么必须单独有这一类：上面所有用例都把 `sf_glass.glass_covers_main` 整个
    patch 掉，断言的是"调用方什么时候去问、用哪两个窗口号问"。而真正决定这套
    兜底成不成立的是**问到窗口列表之后那几行** —— CGWindowListCopyWindowInfo
    返回的列表越靠前越在上，所以玻璃的下标小于主窗才算"盖住了"。这层方向一旦
    写反，看门狗会每 5 秒无谓全量置顶并刷取证日志，而现有用例一个都不会红。
    这里用假 Quartz 喂进 sys.modules，不碰真实 WindowServer。
    """

    NUM = "kCGWindowNumber"
    B = "kCGWindowBounds"

    class _FakeQuartz:
        # 常量必须齐：真函数用 Quartz.kCGWindowNumber 取窗口号，缺任何一个都会
        # 落进它的 except Exception → 一律返 False，用例就会"因为错的原因通过"
        # （v1.4.27 补测时真的踩到过一次）。
        # v1.4.33 起同一个函数还会读 kCGWindowBounds 问"两窗真实错位多少"，
        # 这个常量缺了就会把**顺序**也一起判成不可信，所以一并补齐。
        kCGWindowListOptionOnScreenOnly = 1 << 0
        kCGNullWindowID = 0
        kCGWindowNumber = "kCGWindowNumber"
        kCGWindowBounds = "kCGWindowBounds"

        def __init__(self, infos):
            self._infos = infos
            self.calls = []

        def CGWindowListCopyWindowInfo(self, option, after):
            self.calls.append((option, after))
            return self._infos

    def _probe(self, infos, main=7001, glass=7002):
        fake = self._FakeQuartz(infos)
        with mock.patch.dict(sys.modules, {"Quartz": fake}):
            return sf_glass.glass_covers_main(main, glass), fake

    def _real(self, infos):
        """同一个假窗口列表，问"顺序 + 真实错位"两问（v1.4.33 新入口）。"""
        with mock.patch.dict(sys.modules, {"Quartz": self._FakeQuartz(infos)}):
            return sf_glass.glass_main_real_state(7001, 7002)

    def _list(self, *numbers):
        # 不带 bounds：真实环境里 CGWindowList 一定带，但"顺序"这一问不该依赖它，
        # 这里刻意留空，正好把"缺 bounds 时顺序照判"钉住（见 _real 那条用例）。
        return [{self.NUM: n} for n in numbers]

    def _listb(self, *pairs):
        """(窗口号, x, y) 形式：给"真实错位"那一问喂坐标。"""
        return [{self.NUM: n, self.B: {"X": x, "Y": y, "Width": 184, "Height": 224}}
                for n, x, y in pairs]

    def test_main_ahead_is_not_covered(self):
        """列表里主窗在前（越靠前越在上）→ 没盖住，这是稳态。"""
        covered, fake = self._probe(self._list(7001, 7002))
        self.assertFalse(covered, "主窗在上却判成被盖住：每 5 秒白做一次全量置顶")
        self.assertEqual(fake.calls, [(self._FakeQuartz.kCGWindowListOptionOnScreenOnly,
                                       self._FakeQuartz.kCGNullWindowID)],
                         "必须只查在屏窗口，否则拿到的前后顺序没有意义")

    def test_glass_ahead_is_covered(self):
        """玻璃在前、主窗在后 → 就是用户看到的空壳，必须报"盖住了"。"""
        covered, _ = self._probe(self._list(7002, 7001))
        self.assertTrue(covered, "方向判反了：真正的空壳会被看门狗漏掉")

    def test_other_windows_between_do_not_matter(self):
        """两窗之间夹着别的进程的窗口，不影响这一对的前后关系判定。"""
        covered, _ = self._probe(self._list(7001, 4242, 7002))
        self.assertFalse(covered)
        covered, _ = self._probe(self._list(4242, 7002, 7001))
        self.assertTrue(covered)

    def test_one_window_missing_does_not_guess(self):
        """只查到一窗（另一窗此刻不在屏，例如刚被 orderOut）→ 不下结论。

        宁可这一轮不纠正，也不能凭"查不到"就断定顺序倒了 —— 误判会让置顶每 5 秒
        白做一轮，而漏判只是延后到下一次到点。
        """
        covered, _ = self._probe(self._list(7001, 4242))
        self.assertFalse(covered)

    def test_windowserver_error_degrades_silently(self):
        """查询抛异常（非 macOS / 权限 / Quartz 缺失）→ 一律 False，绝不让复核崩掉。"""
        class _Boom(self._FakeQuartz):
            def CGWindowListCopyWindowInfo(self, option, after):
                raise OSError("WindowServer unavailable")

        with mock.patch.dict(sys.modules, {"Quartz": _Boom([])}):
            self.assertFalse(sf_glass.glass_covers_main(7001, 7002))
            self.assertIsNone(sf_glass.glass_main_real_state(7001, 7002))

    # ---- v1.4.33：同一次查询顺带回答"背景层真的错位了吗" ----------------------

    def test_desync_measured_from_real_bounds(self):
        """两窗真实坐标差 453px → desync_px 就是 453（用户截图里那个量级）。

        守的是"错位幅度算的是谁"：必须算 WindowServer 给的两条 bounds，
        而不是 `gw.frame()` 与 `window.frame()` —— 后者比的是两个**同样过时**的
        应用侧模型，本机实测真实错位 11px 时它们完全相等（glass_desync_audit 的
        blind_samples），那正是永久错位能躲过所有复核的原因。
        """
        st = self._real(self._listb((7001, 900, 300), (7002, 447, 300)))
        self.assertIsNotNone(st)
        self.assertEqual(st["desync_px"], 453.0)
        self.assertFalse(st["glass_on_top"])

    def test_order_still_judged_when_bounds_missing(self):
        """缺 bounds 时 desync 置 None，但**顺序照判**：不为新问题弄丢已验证的旧能力。"""
        st = self._real(self._list(7002, 7001))
        self.assertIsNotNone(st, "两窗都在列表里，顺序这一问就该有答案")
        self.assertTrue(st["glass_on_top"])
        self.assertIsNone(st["desync_px"])


class TestSteadyStateFault(_WinCase):
    """v1.4.33：稳态体检通过后的"用户实际看到什么"复核。

    为什么单独有这一类：`_top_state_ok` 里 `gw.frame() != window.frame()` 比的是
    两个应用侧模型。系统接管拖动时模型本身可以落后几百像素，于是"内容窗被拖走了、
    背景层留在原地"这种用户一眼看得见的故障，在模型眼里是"两窗完全一致"——
    判据看不见，纠正链就永远不启动，错位成为永久状态。
    """

    def _win_with_pair(self):
        import sf_glass
        win = self.make_win()
        behavior = sf_glass.public_overlay_collection_behavior()
        frame = _FakeRect("same")
        main = _NumberedWindow(7001, level=25, behavior=behavior, frame=frame)
        glass = _NumberedWindow(7002, level=25, behavior=behavior, frame=frame)
        win._glass_window = glass
        _patch_objc(self, win, main)
        # 本类断言的是"问到答案之后怎么判"，不是"什么时候去问"（后者由
        # test_order_probe_is_throttled 守）。把节流清零，免得用例因为
        # "节流没到期所以返回 None"而**因为错的原因通过**。
        win._last_z_order_check = 0.0
        return win, main, glass

    def test_real_desync_is_a_fault(self):
        """模型全对、真实位置差 453px → 必须报"错位"，并带上幅度便于事后定位。"""
        win, main, glass = self._win_with_pair()
        self.track_patch(mock.patch.object(
            sf_glass, "glass_main_real_state",
            mock.Mock(return_value={"glass_on_top": False, "desync_px": 453.0})))
        fault = win._steady_state_fault(main, glass)
        self.assertIsNotNone(fault, "真实错位 453px 却没判成故障 → 永久错位又回来了")
        self.assertEqual(fault[0], "glass misaligned")
        self.assertIn("453", fault[1])

    def test_small_desync_is_not_a_fault(self):
        """±2pt 以内不算错位：screencapture 口径的 bounds 本身就有取整抖动。"""
        win, main, glass = self._win_with_pair()
        self.track_patch(mock.patch.object(
            sf_glass, "glass_main_real_state",
            mock.Mock(return_value={"glass_on_top": False, "desync_px": 1.0})))
        self.assertIsNone(win._steady_state_fault(main, glass))

    def test_desync_not_judged_while_follow_timer_runs(self):
        """拖拽期间不判错位：那时背景层由 8ms 原生跟随定时器负责，允许 ≤1 帧滞后
        （本机实测 p95 8px）。在这里报警只会让 250ms 复核去抢它的活。

        断言的是"**根本没去问**"，而不是"返回了 None"—— 后者节流也能造出来，
        会把这条判据变成一条永远过的空话。
        """
        import sf_glass
        win, main, glass = self._win_with_pair()
        probe = mock.Mock(return_value={"glass_on_top": False, "desync_px": 453.0})
        self.track_patch(mock.patch.object(sf_glass, "glass_main_real_state", probe))
        win._glass_follow_timer = object()          # 假装跟随定时器还活着
        self.assertIsNone(win._steady_state_fault(main, glass))
        probe.assert_not_called()

    def test_unknown_state_is_not_a_fault(self):
        """查不到（任一窗不在屏 / 非 macOS / Quartz 异常）→ 本轮不下结论。"""
        win, main, glass = self._win_with_pair()
        self.track_patch(mock.patch.object(sf_glass, "glass_main_real_state",
                                           mock.Mock(return_value=None)))
        self.assertIsNone(win._steady_state_fault(main, glass))

    def test_throttled_tick_is_not_reported_as_blind(self):
        """节流没到期 = **什么都没问**，不能写成"查不到这一对窗"（v1.4.35）。

        实测依据：正式版日志里每条 `re-top: glass above main` 之后恰好 250ms 必跟
        一条 `order probe blind: pair not both on-screen`，而合成顺序复核的节流是
        1000ms —— 那 250ms 的一拍根本没发过查询。一条永不失败的"我没看见"比没有
        这条日志更糟：它把排查引向"窗口确实不在屏上"，而真相是"还没轮到问"。
        """
        win, main, glass = self._win_with_pair()
        win._last_z_order_check = sf_glass.time.monotonic()      # 刚问过，下一拍不该再问  # discipline-ok(R5): 节流时刻只能直接拨这个内部时钟，公开入口没有'刚问过'这个状态可注入
        probe = mock.Mock(return_value=None)
        self.track_patch(mock.patch.object(sf_glass, "glass_main_real_state", probe))
        lines = []
        self.track_patch(mock.patch.object(sf_glass, "log_line",
                                           lambda msg, lvl="INFO": lines.append(msg)))
        self.assertIsNone(win._steady_state_fault(main, glass))  # discipline-ok(R5): 守的是"节流那一拍什么都没问"这条内部判据本身，绕公开入口要等 250ms 时钟、测不到这一支
        probe.assert_not_called()
        self.assertEqual(lines, [],
                         "什么都没问却留下'查不到'的取证 → 日志在说假话：%r" % lines)

    def test_blind_state_leaves_a_trace(self):
        """但"查不到"必须留痕：属性说两窗都在活动 Space，屏幕列表里却没有这一对，
        看门狗从此既看不见也永远不纠正 —— 不写日志的话，下一轮排查只能靠猜。

        只断言"写了这一条"，不断言它顺手去纠正：拿不到事实时乱动比不动更糟。
        """
        win, main, glass = self._win_with_pair()
        self.track_patch(mock.patch.object(sf_glass, "glass_main_real_state",
                                           mock.Mock(return_value=None)))
        lines = []
        self.track_patch(mock.patch.object(sf_glass, "log_line",
                                           lambda msg, lvl="INFO": lines.append(msg)))
        win._steady_state_fault(main, glass)  # discipline-ok(R5): 守的就是这个内部判据本身，绕道公开入口要等 250ms 时钟、测不到这一支
        self.assertTrue(any("order probe blind" in l for l in lines),
                        "复核看不见这一对窗口却一行都不留 → 又回到'毫无痕迹'那一类")


class TestCocoaGuardBeforeObjc(_WinCase):
    """伸手进 AppKit 之前必须先问平台（v1.4.33 崩溃报告直接对应的关卡）。

    为什么值得单独守：offscreen/minimal 下 `winId()` 不是 NSView*，把它当指针喂给
    `objc_object` 是**直接段错误而不是异常** —— 外面套多少层 try/except 都兜不住。
    v1.4.32 产物在 `QT_QPA_PLATFORM=offscreen` 下 rc=139（源码运行同样复现），
    faulthandler 指向 `_force_top` 里那两行 inline 转换，与崩溃报告
    `objc_opt_self ← object_new ← class_call` 逐帧一致。
    这条用例断言的不是"返回 None"，而是**根本没碰 objc 桥** —— 只要碰到了，
    真机上就是又一次"日志三行、进程没了、什么都查不出来"。
    """

    def test_content_ns_window_never_touches_objc_bridge_offscreen(self):
        import sf_glass
        boom = mock.Mock(side_effect=AssertionError("离屏不该碰 objc 桥"))
        self.track_patch(mock.patch.object(sf_glass, "_objc_deps", boom))
        self.track_patch(mock.patch.object(sf_glass, "native_cocoa_active",
                                           lambda: False))
        win = self.make_win()
        self.assertIsNone(sf_glass.content_ns_window(win))
        boom.assert_not_called()

    def test_setup_glass_degrades_without_touching_objc_bridge(self):
        """离屏下 _setup_glass 返回 False → _inject_glass 退回半透明卡，进程照常活着。"""
        import sf_glass
        boom = mock.Mock(side_effect=AssertionError("离屏不该碰 objc 桥"))
        self.track_patch(mock.patch.object(sf_glass, "_objc_deps", boom))
        self.track_patch(mock.patch.object(sf_glass, "native_cocoa_active",
                                           lambda: False))
        win = self.make_win()
        self.assertFalse(sf_glass.GlassMixin._setup_glass(win))
        boom.assert_not_called()

    def test_force_top_is_inert_offscreen(self):
        """置顶复核在离屏下必须整段不动手：它就是 v1.4.32 那次 rc=139 的现场。"""
        import sf_glass
        boom = mock.Mock(side_effect=AssertionError("离屏不该碰 objc 桥"))
        self.track_patch(mock.patch.object(sf_glass, "_objc_deps", boom))
        self.track_patch(mock.patch.object(sf_glass, "native_cocoa_active",
                                           lambda: False))
        win = self.make_win()
        sf_glass.GlassMixin._force_top(win)          # 不得抛、不得段错误
        boom.assert_not_called()


class TestReassertTopEntry(_WinCase):
    """强制置顶的入口必须不带形参（v1.4.27 复核 F2）。

    为什么单独守：`_force_top` 在 v1.4.26 多了 force 形参，而全仓 38 处替身仍是
    `lambda self: None`（含本文件 import 的公共夹具 _WinCase.make_win）。让
    `_restore_window` 直接传 force=True，任何复用这些替身走"缩小到 Dock → 恢复"
    的用例或工具都会 TypeError —— 那一段没有异常兜底，恢复会当场断在
    top_timer.start() 之前。实测复现过一次，故把它钉成用例。
    "跳过体检"这件事本身由 TestForceTopHonoursForce 守。
    """

    def test_restore_window_finishes_with_legacy_stub(self):
        """用旧签名替身跑完整恢复流程：不得抛异常，且必须走到最后的收尾动作。"""
        win = self.make_win()          # 夹具替身就是 lambda self: None，故意不覆写
        win.running = True
        win._minimized = True
        _, glass = _ok_pair()
        win._glass_window = glass
        for name in ("show", "_sync_dot_timer", "_force_fetch", "_poll",
                     "_update_nap_policy", "_is_trading_time"):
            self.track_patch(mock.patch.object(win, name, lambda *a, **k: None))
        for name in ("timer", "top_timer"):
            self.track_patch(mock.patch.object(win, name, mock.Mock()))
        win._restore_window()          # v1.4.26 的写法在这里直接 TypeError
        self.assertEqual(glass.writes, [("orderFront_", None)],
                         "恢复时玻璃层没有被重新上屏")
        win.top_timer.start.assert_called_once()   # 断言置顶之后的收尾全部执行到


class TestRetopLogIsRateLimited(_WinCase):
    """取证日志要有最小间隔（v1.4.27 复核 F5）：修不掉的倒序不该刷满日志。"""

    def _host(self):
        # 只要一个能挂属性的宿主，不需要真窗口
        return sf_glass.GlassMixin.__new__(sf_glass.GlassMixin)

    def _logged(self, clock):
        logged = []
        self.track_patch(mock.patch.object(sf_glass, "time", clock))
        self.track_patch(mock.patch.object(
            sf_glass, "log_line", lambda msg, level="INFO": logged.append(msg)))
        return logged

    def test_same_reason_within_interval_logs_once_and_keeps_count(self):
        """连判 13 次（每 5 秒一次 = 跨 60 秒）只准留 2 条，第二条带上被压掉的条数。

        选 13×5 秒是为了刚好跨过 _RETOP_LOG_INTERVAL 一次；这个常量若被改大，
        本用例会红，届时按新间隔重算采样次数（守的是"同因不刷屏 + 条数不丢"）。
        """
        clock = _FakeMonotonic()
        logged = self._logged(clock)
        host = self._host()
        for _ in range(13):
            sf_glass.GlassMixin._log_retop(host, "glass above main")
            clock.advance(5.0)
        self.assertEqual(len(logged), 2, "刷屏或被压成一条都说明限速逻辑变了：%r" % logged)
        self.assertNotIn("×", logged[0], "第一条不该带抑制计数")
        self.assertTrue(logged[1].endswith("re-top: glass above main ×11"),
                        "被压掉的条数必须随下一条写出，否则看不出这是在反复发生：%r" % logged)

    def test_other_reason_is_not_suppressed(self):
        clock = _FakeMonotonic()
        logged = self._logged(clock)
        host = self._host()
        sf_glass.GlassMixin._log_retop(host, "active space changed")
        clock.advance(5.0)
        sf_glass.GlassMixin._log_retop(host, "glass above main")
        self.assertEqual(logged, ["re-top: active space changed",
                                  "re-top: glass above main"],
                         "两种失效机制不能互相压掉取证")

    def test_alternating_reasons_keep_their_own_clock(self):
        """两种原因**交替**出现时各自 60 秒一条（v1.4.35）。

        实测依据：一个槽位时，`glass above main` 与 `order probe blind` 交替写入，
        每一笔都把对方刚建立的压重计时顶掉，于是 9 秒内打出 9 条同名日志 ——
        "这件事在这一分钟里发生了 40 次"这个强度信息全丢在刷屏里，而它恰恰是
        判断"修不动"的关键证据。
        """
        clock = _FakeMonotonic()
        logged = self._logged(clock)
        host = self._host()
        for _ in range(4):
            sf_glass.GlassMixin._log_retop(host, "glass above main")  # discipline-ok(R5): 压重计时是取证机制的内部状态，没有公开入口能造出"两种原因交替出现"
            clock.advance(5.0)
            sf_glass.GlassMixin._log_retop(host, "order probe blind")  # discipline-ok(R5): 同上：交替出现的第二个原因就是 blind 这条内部原因
            clock.advance(5.0)
        self.assertEqual(logged, ["re-top: glass above main", "re-top: order probe blind"],
                         "交替的两种原因各自只准留一条，实际 %d 条：%r" % (len(logged), logged))


class TestModelResyncBeforeGeometryWrite(_WinCase):
    """v1.4.36：写几何之前，先把 Qt 缓存的原点对齐到 WindowServer 的真值。

    实测现场（本机正式版，Chrome 系统全屏 Space 内）：卡片 (639,348) → 拖到
    (939,508) → 点一下「收起分时走势图」→ 回到 (639,348)。落点偏差正好等于整段
    手势的位移，说明收展那一次 resize / min-max 钳制把**过时**的缓存原点又推回了
    AppKit。桌面上同一条手势实测无此问题（松手后模型与服务端差 0px），所以这一问
    只挂在"马上要写几何"的两个入口，不进 16ms 热路径。
    """

    def _stale(self, win, server_xy, h=224):
        """让"服务端说窗在别处"，而 Qt 缓存不动 —— 复现系统拖拽之后的模型滞后。"""
        win._content_cg_bounds = lambda cnum=None: (float(server_xy[0]),  # discipline-ok(R5): 注入"服务端说窗在别处"只能替换这个内部查询口，产品没有公开 setter
                                                    float(server_xy[1]), 184.0, float(h))

    def test_toggle_chart_does_not_resurrect_stale_origin(self):
        import sf_glass
        win = self.make_win()
        win.move(300, 200)                       # 缓存：还停在拖动前
        self._stale(win, (900, 600))
        self.track_patch(mock.patch.object(sf_glass, "native_cocoa_active", lambda: True))
        self.track_patch(mock.patch.object(sf_glass, "content_ns_window",
                                           lambda w: mock.Mock()))
        win._toggle_chart()  # discipline-ok(R5): 被测动作本身就是这个私有槽（分时图热区的松开回调）
        self.assertEqual((win.x(), win.y()), (900, 600),
                         "收起分时图把卡片搬回拖动前的位置 → 用户报的坐标跳变")

    def test_drag_end_resync_runs_before_edge_snapping(self):
        """松手收口必须先对齐模型再吸附：`_snap_to_edge` 读的就是这个缓存。"""
        import sf_glass
        win = self.make_win()
        win.move(300, 200)
        self._stale(win, (900, 600))
        self.track_patch(mock.patch.object(sf_glass, "native_cocoa_active", lambda: True))
        self.track_patch(mock.patch.object(sf_glass, "content_ns_window",
                                           lambda w: mock.Mock()))
        seen = []
        self.track_patch(mock.patch.object(win, "_reconcile_model_with_server",
                                           lambda why="": seen.append("resync") or True))
        self.track_patch(mock.patch.object(win, "_snap_to_edge",
                                           lambda: seen.append("snap")))
        self.track_patch(mock.patch.object(win, "_glass_follow_end", lambda: None))
        self.track_patch(mock.patch.object(win, "_sync_glass_frame", lambda *a, **k: None))
        # 走一遍松手收口（系统拖拽分支）
        win._sys_drag = True  # discipline-ok(R5): 走系统拖拽收口分支必须置这三位手势状态，没有公开入口能造出"刚做完一次系统拖拽"
        win._drag = False  # discipline-ok(R5): 同上：手势标志复位是 mousePressEvent 的职责，用例直接驱动 release 就要自己摆
        win._resizing = False  # discipline-ok(R5): 同上
        win._press_btn = None  # discipline-ok(R5): 同上
        win._press_chart = False  # discipline-ok(R5): 同上
        from PyQt6.QtCore import QEvent, QPointF, Qt
        from PyQt6.QtGui import QMouseEvent
        local = QPointF(win.rect().center())
        glob = QPointF(win.mapToGlobal(win.rect().center()))
        ev = QMouseEvent(QEvent.Type.MouseButtonRelease, local, glob,
                         Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
                         Qt.KeyboardModifier.NoModifier)
        win.mouseReleaseEvent(ev)
        self.assertEqual(seen, ["resync", "snap"],
                         "吸附跑在对齐之前 = 照着旧位置算贴边落点，卡片会被搬回半路")

    def test_no_move_when_model_already_matches_server(self):
        """模型与服务端一致时一次 move 都不发：正常拖动每次都走这条，不能白写。"""
        import sf_glass
        win = self.make_win()
        win.move(300, 200)
        self._stale(win, (300, 200))
        self.track_patch(mock.patch.object(sf_glass, "native_cocoa_active", lambda: True))
        self.track_patch(mock.patch.object(sf_glass, "content_ns_window",
                                           lambda w: mock.Mock()))
        moves = []
        self.track_patch(mock.patch.object(win, "move", lambda *a: moves.append(a)))
        self.assertFalse(win._reconcile_model_with_server("test"))  # discipline-ok(R5): 守的就是这个内部判据的返回值（"一致时一个 move 都不发"），绕公开入口测不到这一支
        self.assertEqual(moves, [])


class TestGlassOrderedBelowContent(_WinCase):
    """v1.4.35：置顶路径的**最后一步**把"背景层在内容窗之下"显式交给 WindowServer。

    实测现场（本机正式版，Chrome 系统全屏 Space 内）：倒序一旦成立，每 250ms 一拍的
    完整置顶连续 11 秒压不回去（日志 9 条 `re-top: glass above main`，
    tools/fullscreen_state_probe.py 外部旁观记到 12.71 秒倒序），而**换一次 Space
    立刻恢复正常**。两者一对照，问题不在"没人修"，而在"修"依赖 orderFront 的先后，
    而那个先后被同一条路径里后发生的 Space 插入抹掉了。
    `orderWindow:relativeTo:` 是把不变量直接说给 WindowServer，与调用先后无关。
    """

    def _pair(self, main_visible=True):
        win = self.make_win()
        behavior = sf_glass.public_overlay_collection_behavior()
        frame = _FakeRect("same")
        main = _NumberedWindow(7001, level=25, behavior=behavior, frame=frame,
                               visible=main_visible)
        glass = _NumberedWindow(7002, level=25, behavior=behavior, frame=frame)
        win._glass_window = glass  # discipline-ok(R5): 替身玻璃窗只能从这里挂进去，产品侧没有公开 setter
        _patch_objc(self, win, main)
        return win, main, glass

    def test_full_top_orders_glass_below_main_as_its_last_step(self):
        import AppKit
        win, main, glass = self._pair()
        self.track_patch(mock.patch.object(sf_glass, "glass_main_real_state",
                                           _ws(on_top=True)))
        sf_glass.GlassMixin._force_top(win)  # discipline-ok(R5): 置顶由 250ms 定时器驱动，绕开时钟直接驱动才能断言这一拍按什么顺序发了哪些原生调用
        below = [w for w in glass.writes if w[0] == "orderWindow_relativeTo_"]
        self.assertEqual(len(below), 1,
                         "完整置顶没有把背景层显式摆到内容窗之下：%r" % (glass.writes,))
        mode, number = below[0][1]
        self.assertEqual(mode, AppKit.NSWindowBelow)
        self.assertEqual(number, 7001, "参照窗必须是内容窗，不是背景层自己")
        self.assertEqual(glass.writes[-1][0], "orderWindow_relativeTo_",
                         "这一步必须在整条路径的最后 —— 前面抬上来的顺序会被后发生的 "
                         "Space 插入抹掉，放在中间等于没放")

    def test_steady_state_still_does_no_writes(self):
        """顺序没倒时一个写都不发：v1.4.10 的省电结论不能因为这次改动回退。"""
        win, main, glass = self._pair()
        self.track_patch(mock.patch.object(sf_glass, "glass_main_real_state", _ws()))
        sf_glass.GlassMixin._force_top(win)  # discipline-ok(R5): 同上：稳态零写入必须能在同一拍里被证明，等时钟会把断言变成赌时序
        self.assertEqual(main.writes, [])
        self.assertEqual(glass.writes, [])

    def test_no_relative_order_without_a_visible_reference_window(self):
        """内容窗不在屏时不摆：参照物都不在，摆出来的位置是猜的。"""
        win, _, glass = self._pair(main_visible=False)
        self.track_patch(mock.patch.object(sf_glass, "glass_main_real_state",
                                           _ws(on_top=True)))
        sf_glass.GlassMixin._force_top(win)  # discipline-ok(R5): 同上：不在屏参照物这一支没有公开入口可造
        self.assertNotIn("orderWindow_relativeTo_", [w[0] for w in glass.writes],
                         "拿一条不在屏的窗当参照 → %r" % (glass.writes,))


if __name__ == "__main__":
    unittest.main(verbosity=2)
