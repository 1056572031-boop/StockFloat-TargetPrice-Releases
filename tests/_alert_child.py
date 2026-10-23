# -*- coding: utf-8 -*-
"""NSAlert 全链路的子进程替身（被 tests/test_v1417_alert_chain.py 调起）。

为什么单独一个子进程：真·系统弹窗在测试主进程里创建窗口，历史上曾把整个
测试会话冲掉（AppKit 与 Qt 在同一进程里抢事件循环）。弹窗链路又必须真跑一遍
——"弹出→点按钮→回调→收口"这条链只要有一段坏掉，用户点"立即更新"就没反应。
所以放 isolate 的子进程里，父用例只看它打印的标记与退出码。

为了不闪现在你屏幕上：窗口创建成功后立刻把它的原点挪到屏幕外，再做点击。
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import sf_system_alert as s


def _marker(key, value):
    print("%s=%s" % (key, value), flush=True)


def _offscreen(window):
    """挪到屏幕外：链路照跑，人看不见。"""
    from AppKit import NSMakePoint
    window.setFrameOrigin_(NSMakePoint(-12000.0, -12000.0))


# 要量像素的文案，第四项是 center_text：
#   · 反馈提交成功框（用户 2026-10-23 要求"这段文字改成居中"）——编号是 40 个字符的无空格
#     长串，会在窄框里折成多行，最后一行很短，贴左时最显眼；
#   · 更新说明（同一天用户要求"保持贴左"）——编号列表各行长短不一，贴左读才顺。
INK_CASES = (
    ("feedback", "反馈已成功提交。",
     "反馈编号：feedback-af2d7312275c4b849a86f5cb36313384", True),
    ("notes", "发现新版本 v9.9.9。",
     "1. 修复重新打开后，之前的设置没有保留的问题。\n"
     "2. 加快刷新。\n"
     "3. 优化窗口在全屏浏览器里的显示，使弹出的菜单不再消失。", False),
)


def _titles(alert):
    return [str(b.title()) for b in alert.buttons()]


def run_show_and_click():
    import sf_update

    notes = sf_update.numbered_notes_for_display("修复闪退；加快刷新。保留设置。补齐检查。", "9.9.9")
    got = []
    handle = s.show_alert(title="软件更新", text="发现新版本 v9.9.9。",
                          message="发现新版本 v9.9.9。", info=notes,
                          buttons=("暂不更新", "立即更新"), default="立即更新",
                          cancel="暂不更新", on_result=got.append)
    window = handle._window
    alert = handle._alert
    _marker("numbered_notes", str(alert.informativeText()) == notes)
    _marker("note_lines", len(str(alert.informativeText()).splitlines()))
    _marker("visible", window.isVisible())
    _marker("titles", ",".join(_titles(alert)))
    # Esc 键等价物用码点打印（27 = ESC），直接打印不可见
    _marker("esc_codepoint", ord(alert.buttons()[-1].keyEquivalent() or " "))
    _offscreen(window)
    for button in alert.buttons():
        if str(button.title()) == "立即更新":
            button.performClick_(None)
    _marker("result", ",".join(got))
    _marker("visible_after_click", window.isVisible())
    # 防 GC 注册表必须在收口后清空，否则每弹一次窗就永久漏一个窗口对象
    _marker("live_after", len(s.NativeAlert._live))
    return 0 if got == ["立即更新"] and not window.isVisible() else 1


def run_programmatic_close():
    got = []
    handle = s.show_alert(title="软件更新", text="正在检查…",
                          buttons=("好",), on_result=got.append)
    window = handle._window
    _offscreen(window)
    _marker("visible", window.isVisible())
    handle.close()
    handle.close()                       # 重复收口必须安全
    _marker("result", ",".join(got))
    _marker("visible_after_close", window.isVisible())
    _marker("live_after", len(s.NativeAlert._live))
    return 0 if got == [] and not window.isVisible() else 1


def run_missing_button_title():
    """极端情况：按钮标题取不到时，弹窗自己收口但绝不把异常冲穿事件循环。"""
    import objc

    class _BrokenButton:
        def title(self):
            raise RuntimeError("title bridge broken")

    got = []
    handle = s.show_alert(title="软件更新", text="x", buttons=("好",),
                          on_result=got.append)
    _offscreen(handle._window)
    target = handle._target
    target.buttonClicked_(_BrokenButton())
    _marker("result", ",".join(got))
    _marker("visible_after_broken", handle._window.isVisible())
    handle.close()
    return 0                              # 只要没抛出就算通过


def run_progress():
    """真 AppKit 下载进度框：系统进度条挂得上去、刷得动、点取消能收口。

    桩 AppKit 只能证明"我们按顺序调了这些方法"，证明不了 NSAlert 真的接受
    accessoryView、NSProgressIndicator 真的存下数值 —— 那才是用户看得见的东西。
    """
    got = []
    handle = s.show_progress_alert(message="正在下载更新…", info="准备中 0%",
                                   on_result=got.append)
    if handle is None:
        _marker("native", "unavailable")
        return 4
    _offscreen(handle._window)
    alert = handle._alert
    bar = alert.accessoryView()
    _marker("bar_class", type(bar).__name__)
    _marker("indeterminate", bar.isIndeterminate())
    _marker("first_update", handle.set_progress(37, "37%（9.9 / 26.8 MB）"))
    _marker("bar_value", bar.doubleValue())
    _marker("subtext", str(alert.informativeText()))
    _marker("repeat_update", handle.set_progress(37, "37%（9.9 / 26.8 MB）"))
    for button in alert.buttons():
        if str(button.title()) == "取消":
            button.performClick_(None)
    _marker("result", ",".join(got))
    _marker("visible_after_cancel", handle._window.isVisible())
    _marker("late_update_accepted", handle.set_progress(90, "90%"))
    ok = (type(bar).__name__ == "NSProgressIndicator"
          and not bar.isIndeterminate()
          and abs(bar.doubleValue() - 37.0) < 0.01
          and str(alert.informativeText()) == "37%（9.9 / 26.8 MB）"
          and got == ["取消"]
          and not handle._window.isVisible()
          and not handle.set_progress(95, "95%"))
    return 0 if ok else 1


def _geom(handle):
    """量一个真弹窗的内容几何，返回 (图标, 文案段, 底部控件) 三类矩形。"""
    import AppKit

    content = handle._window.contentView()
    content.layoutSubtreeIfNeeded()
    width = content.frame().size.width
    icon, texts, bottom = None, [], []
    for view in content.subviews():
        frame = view.frame()
        if icon is None and isinstance(view, AppKit.NSImageView):
            icon = frame
        elif isinstance(view, AppKit.NSTextField):
            if str(view.stringValue() or "").strip():
                texts.append((view, frame))
        else:
            bottom.append(frame)
    texts.sort(key=lambda item: -item[1].origin.y)     # 上→下（标题在正文上面）
    return width, icon, [item for item in texts], bottom


def _check_case(tag, handle, out):
    """一个内容形态一测：返回 True 表示这一例全过。

    守的四件事（都是用户看得见的）：内容水平居中、图标与文案**纵向不重叠**、
    文案没被裁掉最后一行、按钮仍贴着底部（弹窗长高后不得留一条空白带）。
    为什么必须看内部状态（`handle._window`）：要量的就是真窗口的视图几何，
    它没有任何公开输出可读。
    """
    import AppKit

    width, icon, texts, bottom = _geom(handle)
    if icon is None or not texts:
        out.append("%s:找不到图标或文案" % tag)
        return False
    ok = True

    def cx(frame):
        return frame.origin.x + frame.size.width / 2.0 - width / 2.0

    if abs(cx(icon)) > 1.0 or any(abs(cx(f)) > 1.0 for _v, f in texts):
        out.append("%s:未居中 icon%+.1f %s"
                   % (tag, cx(icon),
                      ",".join("%.1f" % cx(f) for _v, f in texts)))
        ok = False
    boxes = [("icon", icon)] + [("t%d" % i, f) for i, (_v, f) in enumerate(texts)]
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            a, b = boxes[i][1], boxes[j][1]
            ox = (min(a.origin.x + a.size.width, b.origin.x + b.size.width)
                  - max(a.origin.x, b.origin.x))
            oy = (min(a.origin.y + a.size.height, b.origin.y + b.size.height)
                  - max(a.origin.y, b.origin.y))
            if ox > 0.5 and oy > 0.5:
                out.append("%s:%s 与 %s 重叠 %.1fx%.1fpt"
                           % (tag, boxes[i][0], boxes[j][0], ox, oy))
                ok = False
    if icon.origin.y < max(f.origin.y + f.size.height for _v, f in texts):
        out.append("%s:图标没有排在文案上方" % tag)
        ok = False
    for i, (view, frame) in enumerate(texts):
        drawn = view.attributedStringValue().boundingRectWithSize_options_(
            AppKit.NSMakeSize(frame.size.width, 1.0e6), 3).size.height
        if drawn > frame.size.height + 1.0:
            out.append("%s:第%d段文案被裁 drawn=%.1f > box=%.1f"
                       % (tag, i, drawn, frame.size.height))
            ok = False
        if str(view.alignment()) != str(AppKit.NSTextAlignmentCenter):
            out.append("%s:第%d段文案未居中(align=%s)" % (tag, i, view.alignment()))
            ok = False
    if bottom:
        pad = min(f.origin.y for f in bottom)
        if pad > 24.0:
            out.append("%s:按钮离底 %.1fpt（底部留了空白带）" % (tag, pad))
            ok = False
    return ok


def run_centered():
    """v1.4.22 立的目标（图标在上、整体居中）在**两种版式**下都必须成立。

    v1.4.33 补的关键一刀：NSAlert 按文案长短走两套版式 —— 旧用例只测了短文案的
    窄版式；长文案（真实更新说明）走宽版式时图标与标题在同一条横带里，
    只挪水平就把图标压到了标题上（老板 2026-09-24 截图），而用例照样绿。
    所以这里短/长两种文案各测一遍。
    """
    import AppKit

    def offscreen(handle):
        handle._window.setFrameOrigin_(AppKit.NSMakePoint(-12000.0, -12000.0))

    long_info = "\n".join(
        "%d. 第%d条说明：拖动悬浮窗改为交给 macOS 系统驱动，位移由窗口服务器合成，"
        "不再被一次整窗绘制或一次置顶复核卡在两帧之间，消除实测最差的拖拽长尾。" % (n, n)
        for n in range(1, 9))
    cases = (("窄", "正文第一行。\n正文第二行。\n正文第三行。"),
             ("宽", long_info))
    out = []
    passed = True
    for tag, info in cases:
        message = "发现新 Beta 版本 v9.9.9。"
        handle = s.show_alert(title="软件更新", text=message + "\n\n" + info,
                              message=message, info=info,
                              buttons=("暂不更新", "立即更新"), default="立即更新",
                              cancel="暂不更新", icon_path=s.bundled_icon_path())
        try:
            height = handle._window.contentView().frame().size.height
            passed = _check_case(tag, handle, out) and passed
            _marker("height_" + tag, "%.0f" % height)
        finally:
            offscreen(handle)
            handle.close()
    handle = s.show_alert(title="软件更新", text="x", message="标题",
                          buttons=("好",), icon_path=s.bundled_icon_path())
    try:
        rings = [str(b.focusRingType()) for b in handle._alert.buttons()]
    finally:
        offscreen(handle)
        handle.close()
    if not rings or any(r != str(AppKit.NSFocusRingTypeNone) for r in rings):
        out.append("按钮焦点描边未关闭 %s" % rings)
        passed = False
    _marker("problems", " | ".join(out) if out else "none")
    _marker("centered", "ok" if passed else "bad")
    return 0 if passed else 1


def run_ink(out_dir):
    """把真 NSAlert 的文案画进位图，交给父进程量每一行墨迹的左右留白。

    为什么必须量像素：控件、cell 的对齐属性读回都是居中，可真正画字时文案自带的段落样式
    （系统默认写着自然对齐）压过它们，多行文案每一行都贴左（2026-10-23 反馈提交成功框，
    本机读回两侧留白差 17~366 像素）。属性读回证明不了字画在哪儿，只有像素能。

    窗口一次都不上屏（present 换成空操作）：cacheDisplayInRect 对没显示的窗口照样出图，
    也就不会抢焦点。图标和按钮区藏起来，位图里只剩文字，父进程按透明度认墨迹，
    浅色和深色外观下都成立。
    """
    import AppKit

    s.NativeAlert.present = lambda self: True
    s.center_window = lambda window: None
    for tag, message, info, center in INK_CASES:
        handle = s.show_alert(title=message, text=message + "\n\n" + info,
                              message=message, info=info, buttons=("好",),
                              icon_path=s.bundled_icon_path(), center_text=center)
        content = handle._window.contentView()  # discipline-ok(R5): 要量的就是真窗口的内容视图，无公开出口
        for view in content.subviews():
            if not isinstance(view, AppKit.NSTextField):
                view.setHidden_(True)
        rect = content.bounds()
        rep = content.bitmapImageRepForCachingDisplayInRect_(rect)
        content.cacheDisplayInRect_toBitmapImageRep_(rect, rep)
        path = str(pathlib.Path(out_dir) / (tag + ".png"))
        data = rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, None)
        data.writeToFile_atomically_(path, True)
        _marker("png_" + tag, path)
        handle.close()
    return 0


def run_probe():
    """预检：这台机器/这个会话能不能真创建系统弹窗窗口。

    没有窗口服务（SSH、CI）时这里会失败或异常，父用例据此**整体跳过**，
    而不是把"环境没有屏幕"误报成"弹窗链路坏了"。
    """
    handle = s.show_alert(title="预检", text="预检", buttons=("好",),
                          on_result=lambda _b: None)
    _offscreen(handle._window)
    visible = bool(handle._window.isVisible())
    handle.close()
    _marker("probe", "ok" if visible else "no-window")
    return 0 if visible else 4


def run_position():
    """v1.4.38（用户 2026-10-08 要求）：检查更新相关的窗口必须落在**屏幕正中**。

    改前实测（本机 1470x956 单屏，真弹出来量）：「正在检查更新」的窗口中心比屏幕
    可用区中心偏左 384pt、偏上 178pt；带长更新说明的那版偏左 254pt、偏上 70pt。
    桩 AppKit 永远测不出这件事 —— NSAlert 会在我们摆完之后按自己的版式再定一次位，
    只有真窗口 + 真屏幕能回答「用户实际看见它在哪儿」。

    量的是相对**目标屏可用区**（已扣菜单栏与 Dock）的偏移：正中才是用户要的
    「屏幕正中间」，按整块屏算会被 Dock 顶得肉眼可见地偏上。
    """
    handles = []

    def win(handle):
        return handle._window  # discipline-ok(R5): 要量的就是这个窗口的屏幕坐标，无公开出口

    def offset(handle):
        """(dx, dy)：窗口中心 相对 目标屏可用区中心 的偏移；取不到屏返回 None。"""
        screen = s.target_screen()
        if screen is None:
            return None
        vf = screen.visibleFrame()
        fr = win(handle).frame()
        return (fr.origin.x + fr.size.width / 2.0 - (vf.origin.x + vf.size.width / 2.0),
                fr.origin.y + fr.size.height / 2.0 - (vf.origin.y + vf.size.height / 2.0),
                win(handle).screen() is screen)

    def check(tag, handle):
        dx = offset(handle)
        if dx is None:
            out.append("%s:取不到目标屏" % tag)
            return
        if not dx[2]:
            out.append("%s:没落在目标屏上" % tag)
            return
        if abs(dx[0]) > TOL or abs(dx[1]) > TOL:
            out.append("%s:偏 %+.1f,%+.1fpt" % (tag, dx[0], dx[1]))
        _marker(tag, "%+.1f,%+.1f" % (dx[0], dx[1]))

    TOL = 1.0
    out = []
    long_info = "\n".join(
        "%d. 第%d条说明：拖动悬浮窗改为交给 macOS 系统驱动，位移由窗口服务器合成。" % (n, n)
        for n in range(1, 9))
    # 1) 手动检查更新时的「正在检查更新」——用户截图里那一次
    h = s.show_alert(title="软件更新", text="正在检查更新，请稍候…",
                     message="正在检查更新，请稍候…", buttons=("好",),
                     icon_path=s.bundled_icon_path())
    handles.append(h)
    check("检查中", h)
    # 2) 带长更新说明的新版确认框（窗口会被 _center_content 长高，落点要跟着补）
    h = s.show_alert(title="软件更新", text="发现新版本 v9.9.9。\n\n" + long_info,
                     message="发现新版本 v9.9.9。", info=long_info,
                     buttons=("暂不更新", "立即更新"), default="立即更新",
                     cancel="暂不更新", icon_path=s.bundled_icon_path())
    handles.append(h)
    check("长说明", h)
    # 3) 下载进度框刷进度：副文案换行/长高后仍须在正中
    h = s.show_progress_alert(message="正在下载更新…", info="准备中 0%")
    handles.append(h)
    if h is None:
        out.append("进度框:原生路径不可用")
    else:
        h.set_progress(37, "37%（9.9 / 26.8 MB）")
        check("进度框", h)
    # 4) 登记了悬浮窗所在屏（Qt 全局坐标）后仍须居中；脏值不得把弹窗弄丢
    screen = s.target_screen()
    if screen is None:
        out.append("锚点:取不到屏")
    else:
        f = screen.frame()
        s.set_screen_anchor(f.origin.x + f.size.width / 2.0,
                            f.origin.y + f.size.height / 2.0)
        h = s.show_alert(title="软件更新", text="当前已是最新版本。",
                         message="当前已是最新版本。", buttons=("好",),
                         icon_path=s.bundled_icon_path())
        handles.append(h)
        check("带锚点", h)
        s.set_screen_anchor("不是数字", None)
        h = s.show_alert(title="软件更新", text="暂时无法检查更新。",
                         message="暂时无法检查更新。", buttons=("好",),
                         icon_path=s.bundled_icon_path())
        handles.append(h)
        check("脏锚点", h)
    for h in handles:
        _offscreen(h._window)  # discipline-ok(R5): 收口前必须把真窗口挪出屏幕
        h.close()
    _marker("problems", " | ".join(out) if out else "none")
    _marker("position", "ok" if not out else "bad")
    return 0 if not out else 1


def run_presentation():
    """Read back the real native policy rather than trusting bridge call arguments."""
    import AppKit
    from sf_glass import TOOL_WINDOW_LEVEL
    handle = s.show_alert(title="软件更新", text="正在检查更新，请稍候…")
    window = handle._window  # discipline-ok(R5): native boundary under test
    _offscreen(window)
    behavior = int(window.collectionBehavior())
    _marker("joins_all", bool(behavior & AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces))
    _marker("fullscreen_aux", bool(behavior & AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary))
    _marker("moves_space", bool(behavior & AppKit.NSWindowCollectionBehaviorMoveToActiveSpace))
    _marker("above_main", int(window.level()) == TOOL_WINDOW_LEVEL)
    _marker("hides", bool(window.hidesOnDeactivate()))
    handle.close()
    _marker("reopens_closed", handle.present())
    return 0


def run_nobutton():
    """buttons=() 的纯进度提示：读回真实 NSAlert，不信任调用参数。

    NSAlert 在 layout 时会自己补一个「OK」（2026-10-22 实测），所以判据是：按钮被藏起来、
    禁用、没有回车键位；窗口比带按钮的那张矮（没有留下空白带）；文案没有压到窗口底边外。
    """
    with_button = s.show_alert(title="t", text="正在上传反馈，请稍候。", buttons=("好",))
    _offscreen(with_button._window)  # discipline-ok(R5): 真窗口必须先挪出屏幕再量
    tall = float(with_button._window.frame().size.height)  # discipline-ok(R5): native boundary under test
    with_button.close()
    handle = s.show_alert(title="t", text="正在上传反馈，请稍候。", buttons=())
    window = handle._window  # discipline-ok(R5): native boundary under test
    _offscreen(window)
    alert = handle._alert  # discipline-ok(R5): native boundary under test
    buttons = list(alert.buttons())
    _marker("all_hidden", all(bool(b.isHidden()) for b in buttons))
    _marker("all_disabled", all(not bool(b.isEnabled()) for b in buttons))
    _marker("no_key_equivalent", all(str(b.keyEquivalent()) == "" for b in buttons))
    height = float(window.frame().size.height)
    _marker("shorter_than_with_button", height < tall)
    lowest = min(float(v.frame().origin.y) for v in window.contentView().subviews()
                 if not v.isHidden() and (not hasattr(v, "stringValue") or str(v.stringValue() or "").strip()))
    _marker("bottom_gap_ok", 0.0 <= lowest <= s.NativeAlert._BOTTOM_GAP + 1.0)  # discipline-ok(R5): layout constant under test
    handle.close()
    return 0


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        if mode == "nobutton":
            sys.exit(run_nobutton())
        if mode == "probe":
            sys.exit(run_probe())
        if mode == "progress":
            sys.exit(run_progress())
        if mode == "click":
            sys.exit(run_show_and_click())
        if mode == "close":
            sys.exit(run_programmatic_close())
        if mode == "broken":
            sys.exit(run_missing_button_title())
        if mode == "centered":
            sys.exit(run_centered())
        if mode == "position":
            sys.exit(run_position())
        if mode == "presentation":
            sys.exit(run_presentation())
        if mode == "ink":
            sys.exit(run_ink(sys.argv[2]))
    except Exception as exc:                       # noqa: BLE001
        _marker("error", repr(exc))
        sys.exit(2)
    print("usage: _alert_child.py click|close|broken")
    sys.exit(3)
