#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools/glass_occlusion_probe.py —— 主动把背景层压到内容窗之上，看看门狗多久救回来。

要回答的问题
------------
用户第二次报"主界面无内容"（截图里只剩深色玻璃底 + 一个目标价气泡）。
目标价气泡 `TargetBubble` 是**独立 NSWindow**，它还在、主窗内容不在 —— 这与
"玻璃层压住了内容窗"完全吻合，而与我刚修的"背景层位置脱节"是**两回事**。

v1.4.26 给这一类失效装了看门狗（`glass_main_real_state` 判序 → 完整置顶纠正）。
本脚本不猜它有没有用：**当场制造一次倒序，然后计时看它多久把内容窗救回来**。
如果 10 秒内不恢复，说明看门狗在这台机器上根本没生效，那才是要修的东西。

判据
----
    inject_to_inverted_s   注入到 WindowServer 真正反映倒序的耗时（实测 0.01~0.03s）
    recovered_after_s      顺序回到"内容在上"所耗的秒数；None = 观察窗内没恢复
                           —— 这才是本脚本的主判据（v1.4.33 实测：节流 5s 时 4.46s，
                              节流 1s 时 0.57s）
    content_ink_ratio /
    glass_ink_ratio        逐窗 screencapture 的"亮像素占比"。
                           ⚠ **这一条不能用来区分哪个窗有内容**：本机实测
                           内容窗 6.9% / 玻璃层 93.6%，方向与"玻璃层亮像素≈0"的
                           直觉相反 —— 玻璃层是 NSVisualEffectView，它采样桌面壁纸，
                           壁纸亮它就亮；而本脚本跑的是无行情数据的源码实例，内容窗
                           本来就只画了少量文字。老记录里那条经验按当时的壁纸与
                           数据状态成立，换环境就不成立。留着只作现场参考，
                           **判序仍以两窗的真实窗口号为准**。

用法
----
    ./.venv/bin/python tools/glass_occlusion_probe.py
    ./.venv/bin/python tools/glass_occlusion_probe.py --interval 1.0   # 临时改节流做 A/B
    ./.venv/bin/python tools/glass_occlusion_probe.py --json

⚠ 会在屏幕上短暂出现一个真实悬浮窗（源码实例，落盘路径全部重定向临时目录）。
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time

PROJ = pathlib.Path(__file__).resolve().parents[1]

HOME = pathlib.Path(tempfile.mkdtemp(prefix="sf_occl_"))
os.environ["QT_QPA_PLATFORM"] = "cocoa"
os.environ["SF_CONFIG_PATH"] = str(HOME / "config.json")
os.environ["SF_LOG_PATH"] = str(HOME / "debug.log")
os.environ["SF_UNIVERSE_PATH"] = str(HOME / "securities.json")
os.environ["SF_LAST_QUOTE_PATH"] = str(HOME / "last_quote.json")
os.environ["SF_SINGLE_SOCK"] = str(HOME / "single.sock")
os.environ["SF_SEED_FIRST_SCREEN"] = "0"
sys.path.insert(0, str(PROJ))


def order_and_bounds(main_num, glass_num):
    """(主窗是否在玻璃之前, 两窗 bounds)。列表越靠前越在上。"""
    import Quartz
    lst = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly,
                                            Quartz.kCGNullWindowID)
    idx, bnds = {}, {}
    for i, e in enumerate(lst):
        n = int(e.get("kCGWindowNumber", -1))
        if n in (main_num, glass_num):
            idx[n] = i
            bnds[n] = e.get("kCGWindowBounds")
    if main_num not in idx or glass_num not in idx:
        return None, bnds
    return idx[main_num] < idx[glass_num], bnds


def window_ink(num):
    """抓**该窗自身**像素，返回"有内容"的像素占比。

    为什么不能只看整屏截图：整屏分不清那一块深色是玻璃还是内容窗自己画的底。
    逐窗抓取（screencapture -l<窗口号>）才拿得到每个窗口自己的表面。
    内容窗有文字与曲线 → 占比高；纯毛玻璃层 → 接近 0。
    """
    p = os.path.join(HOME, "w%d.png" % num)
    r = subprocess.run(["/usr/sbin/screencapture", "-x", "-o", "-l%d" % num, p],
                       capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(p):
        return None
    from PyQt6.QtGui import QImage
    im = QImage(p)
    if im.isNull():
        return None
    w, h = im.width(), im.height()
    # 走 pixelColor 而不是 constBits()+bytes(ptr)：后者要先把 ctypes voidptr
    # setsize 再转 bytes（本仓踩过 IndexError），而 mypy 的存根认为 bytes(voidptr)
    # 没有匹配重载 —— 为了一个探针去动全仓"不用 type: ignore"的规矩不划算。
    # 代价是慢，所以按 4px 网格采样：判"有没有画内容"不需要逐像素。
    step = 4
    ink = 0
    total = 0
    for y in range(0, h, step):
        for x in range(0, w, step):
            c = im.pixelColor(x, y)
            total += 1
            if c.alpha() <= 40:
                continue
            # "有内容" = 不透明且明显偏离纯黑底：内容窗有文字/曲线/图标，
            # 纯毛玻璃层几乎不画亮像素。
            if (c.red() + c.green() + c.blue()) / 3.0 > 70:
                ink += 1
    return round(ink / max(total, 1), 5)


def main(argv=None):
    ap = argparse.ArgumentParser(description="玻璃层压住内容窗：看门狗恢复计时")
    ap.add_argument("--observe", type=float, default=10.0, help="注入倒序后观察秒数")
    ap.add_argument("--interval", type=float, default=0.0,
                    help="临时改写合成顺序复核的节流秒数（0=用产品默认值）。"
                         "用来证明"
                         "\"恢复要 4.5s\"到底是节流造成的还是别的")
    ap.add_argument("--repeat", type=int, default=1, help="注入几次（每次重新计时）")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    def say(*x):
        if not a.json:
            print(*x, flush=True)

    from PyQt6.QtWidgets import QApplication

    def pump(ms=20):
        """泵事件但不空转：processEvents() 单独跑会把 CPU 打满、量不准
        （本仓功耗底座的既有结论），所以配一个 20ms 的短睡。"""
        app.processEvents()
        time.sleep(ms / 1000.0)

    import sf_ui_render as R
    import stock_float_window as m

    app = QApplication.instance() or QApplication([])
    R.load_bundled_fonts()
    win = m.FloatWindow()
    win.move(520, 260)
    win.show()
    deadline = time.time() + 8.0
    while time.time() < deadline:
        pump(30)
        if getattr(win, "_glass_window", None) is not None:
            break
    if getattr(win, "_glass_window", None) is None:
        print("!! 玻璃层没建起来，无法核验", file=sys.stderr)
        return 2

    import objc
    from ctypes import c_void_p
    nsw = objc.objc_object(c_void_p=int(win.winId())).window()
    main_num, glass_num = int(nsw.windowNumber()), int(win._glass_window.windowNumber())
    say("内容窗 %d / 玻璃窗 %d" % (main_num, glass_num))
    if a.interval > 0:
        import sf_glass
        sf_glass._Z_ORDER_CHECK_INTERVAL = a.interval
        say("已把合成顺序复核节流临时改为 %.2fs（产品默认 5.0s）" % a.interval)

    pump(600)
    ok_before, _ = order_and_bounds(main_num, glass_num)
    say("注入前：内容在上 = %s" % ok_before)

    # 注入倒序：把玻璃层最后 orderFront —— 与 v1.4.26 记录里"唯一能造出空壳"的
    # 外部 orderFront 路径同一手法，也是第三方全屏时 WindowServer 替我们做的事。
    t_inject = time.monotonic()
    win._glass_window.orderFront_(None)

    # 顺序变更要等 WindowServer 落一帧才在 CGWindowList 里看得见（实测：注入后
    # 立刻读仍是旧值）。所以先轮询到"确实倒了"，再从那一刻起算恢复耗时；
    # 一注就读会把"还没生效"误判成"注入失败"。
    inverted_at = None
    ok_now = ok_before
    probe_end = time.time() + 2.0
    while time.time() < probe_end:
        pump(20)
        ok_now, _ = order_and_bounds(main_num, glass_num)
        if ok_now is False:
            inverted_at = time.monotonic()
            break
    say("注入后：内容在上 = %s（False 即用户此刻看到\"只剩背景\"），耗时 %.2fs 生效" % (
        ok_now, 0.0 if inverted_at is None else inverted_at - t_inject))
    if inverted_at is None:
        say("!! 2 秒内没观察到倒序 —— 要么注入没生效，要么看门狗在 250ms 一拍的"
            "复核里当场就纠正了（那就是好消息，但本脚本测不出恢复延迟）")
        return 2

    samples = [(0.0, False)]
    recovered_after = None
    end = time.time() + a.observe
    while time.time() < end:
        pump(20)
        ok, _bn = order_and_bounds(main_num, glass_num)
        t = round(time.monotonic() - inverted_at, 2)
        if len(samples) < 6 or ok is not samples[-1][1]:
            samples.append((t, ok))
        if ok is True and recovered_after is None:
            recovered_after = t
            say("→ %.2fs 后看门狗把顺序纠正回来了" % t)
            break
    if recovered_after is None:
        say("→ %.0fs 内**没有**恢复：看门狗在这台机器上没生效" % a.observe)

    pump(400)
    ink_main = window_ink(main_num)
    ink_glass = window_ink(glass_num)
    ok_end, bnds = order_and_bounds(main_num, glass_num)

    res = {
        "inject_to_inverted_s": round(inverted_at - t_inject, 2),
        "ok_before": ok_before,
        "recovered_after_s": recovered_after,
        "order_at_end": ok_end,
        "content_ink_ratio": ink_main,
        "glass_ink_ratio": ink_glass,
        "timeline": samples,
        "bounds": {str(k): [round(float(v["X"])), round(float(v["Y"])),
                            round(float(v["Width"])), round(float(v["Height"]))]
                   for k, v in bnds.items() if v},
        "log_retop": [],
        "verdict": None,
    }
    lg = pathlib.Path(HOME) / "debug.log"
    if lg.exists():
        res["log_retop"] = [l for l in lg.read_text(
            encoding="utf-8", errors="replace").splitlines() if "re-top" in l][-6:]

    if recovered_after is None:
        res["verdict"] = ("注入倒序后 %.0fs 内没有恢复 → 看门狗没生效，这才是\"只剩背景\""
                          "复发的直接原因；顺带看 log_retop 里有没有抓到" % a.observe)
    elif recovered_after > 5.5:
        res["verdict"] = "%.2fs 才恢复（>5.5s ≈ 复核节流到期）→ 能用但慢，用户看得见" % recovered_after
    else:
        res["verdict"] = "%.2fs 内恢复 → 看门狗有效，本次复发另有原因" % recovered_after
    print(json.dumps(res, ensure_ascii=False, indent=2))
    try:
        win.quit()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
