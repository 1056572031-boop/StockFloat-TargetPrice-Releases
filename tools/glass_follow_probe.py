# -*- coding: utf-8 -*-
"""tools/glass_follow_probe.py —— 真鼠标拖窗口时，毛玻璃背景掉队多少像素。

为什么需要它
------------
主窗是**两个同级 NSWindow**：内容窗 + 独立毛玻璃背景窗。v1.4.31 起把"拖动窗口"
交给 macOS 系统（`system_drag`），位移不再由本进程逐帧执行 —— 于是出现一个此前不
存在的问题：**系统把内容窗挪走了，本进程有没有及时把背景窗也挪过去？**

这个问题在合成事件里问不出来：Qt 的 `startSystemMove()` 要求"当前正在处理一个
真实指针事件"，注入的 QMouseEvent 下它直接返回 False（实测）。所以只能发**真的
鼠标事件**（HID 层 CGEventPost），并且用**另一个进程**逐帧采样两窗位置 —— 因为
系统拖拽期间本进程可能正卡在 AppKit 的模态事件循环里，自己量不到自己。

判据
----
  max_desync_px   拖拽过程中 |内容窗坐标 - 背景窗坐标| 的最大值
  final_desync_px 松手后稳定 300ms 的差值（必须为 0，否则就是永久错位）
  moved_px        窗口真的被拖走了多少（防呆：没动 = 事件没被接受/权限缺失，
                  此时所有"零掉队"都是假的）

用法
----
    ./.venv/bin/python tools/glass_follow_probe.py            # 自动找窗口并拖一次
    ./.venv/bin/python tools/glass_follow_probe.py --dx 240 --steps 40 --json

需要"辅助功能/输入监控"权限：没有权限时事件会被系统丢弃，脚本会如实报
"窗口没动"，不会给出一个好看的零掉队结论。
"""
import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
PY = os.path.join(PROJ, ".venv", "bin", "python")


OWNER = os.environ.get("SF_PROBE_OWNER", "实时股价")   # 源码实例的进程名是 Python


def _windows():
    """返回 [(窗口号, x, y, w, h)]，按屏幕前后顺序（越靠前越在上）。"""
    import Quartz
    lst = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly,
                                            Quartz.kCGNullWindowID)
    out = []
    for e in lst:
        if OWNER not in str(e.get("kCGWindowOwnerName") or ""):
            continue
        b = e.get("kCGWindowBounds")
        out.append((int(e.get("kCGWindowNumber")), float(b["X"]), float(b["Y"]),
                    float(b["Width"]), float(b["Height"])))
    return out


def sample(seconds, gap_s):
    """子进程侧：高频采样两窗位置，打印 JSON 数组。"""
    t0 = time.perf_counter()
    rows = []
    while time.perf_counter() - t0 < seconds:
        ws = _windows()
        if len(ws) >= 2:
            rows.append([(time.perf_counter() - t0) * 1000.0]
                        + [(w[0], round(w[1], 1), round(w[2], 1)) for w in ws[:2]])
        time.sleep(gap_s)
    print(json.dumps(rows))
    sys.stdout.flush()


def drag(dx, dy, steps, hold_ms, no_return=False):
    """父进程侧：发真实 HID 鼠标事件拖一次，同时让子进程采样。"""
    import Quartz
    ws = _windows()
    if len(ws) < 2:
        print("没找到两条在屏窗口（程序没在运行？），无法验证。", file=sys.stderr)
        return 2
    content, glass = ws[0], ws[1]
    # 按下点：内容窗左上角空白（避开右上四个按钮与中下部的分时图热区）
    sx, sy = content[1] + 20.0, content[2] + 5.0
    log = lambda *a: print("[glass]", *a, flush=True)
    log("内容窗 %s @ (%.0f,%.0f)  背景窗 %s @ (%.0f,%.0f)" % (
        content[0], content[1], content[2], glass[0], glass[1], glass[2]))
    pos = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
    log("当前鼠标位置 (%.0f,%.0f) —— 拖完会放回这里" % (pos.x, pos.y))

    child = subprocess.Popen([PY, os.path.abspath(__file__), "--sample",
                              "--seconds", "4.0", "--gap", "0.004"],
                             cwd=PROJ, stdout=subprocess.PIPE, text=True)

    def post(kind, x, y, button):
        ev = Quartz.CGEventCreateMouseEvent(None, kind, (x, y), button)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)

    DOWN = Quartz.kCGMouseButtonLeft
    post(Quartz.kCGEventLeftMouseDown, sx, sy, DOWN)
    time.sleep(0.05)
    for i in range(1, steps + 1):
        post(Quartz.kCGEventLeftMouseDragged, sx + dx * i / steps,
             sy + dy * i / steps, DOWN)
        time.sleep(hold_ms / 1000.0)
    time.sleep(0.25)
    post(Quartz.kCGEventLeftMouseUp, sx + dx, sy + dy, DOWN)

    out, _ = child.communicate(timeout=20)
    rows = json.loads(out or "[]")
    if not rows:
        log("采样为空：子进程没抓到两窗同时在屏的帧。")
        return 1
    cid = rows[0][1][0]
    desync = [max(abs(r[1][1] - r[2][1]), abs(r[1][2] - r[2][2])) for r in rows
              if len(r) == 3 and r[1][0] == cid and r[2][0] != cid]
    start = rows[0][1][1]
    end = rows[-1][1][1]
    moved = abs(end - start)
    tail = desync[-15:]
    res = {
        "frames": len(rows),
        "moved_px": round(moved, 1),
        "max_desync_px": round(max(desync), 1) if desync else None,
        "p95_desync_px": round(sorted(desync)[int(len(desync) * 0.95) - 1], 1)
                         if len(desync) > 5 else None,
        "final_desync_px": round(max(tail), 1) if tail else None,
        "target_dx": dx,
        "verdict": None,
    }
    if moved < max(20.0, abs(dx) * 0.3):
        res["verdict"] = "窗口没被拖走（真实鼠标事件未被接受：多半是缺辅助功能/输入监控权限）→ 本次数据无效"
    elif res["final_desync_px"] and res["final_desync_px"] > 1.0:
        res["verdict"] = "松手后背景仍与内容错位 %.1f px → 必须修" % res["final_desync_px"]
    elif (res["max_desync_px"] or 0) > 8.0:
        res["verdict"] = "拖拽中背景最多掉队 %.1f px（约 %.1f 帧）→ 看得见，需要收紧" % (
            res["max_desync_px"], res["max_desync_px"] / 16.7)
    else:
        res["verdict"] = "跟得上：拖拽中最大掉队 %.1f px、松手后 %.1f px" % (
            res["max_desync_px"] or 0.0, res["final_desync_px"] or 0.0)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    if no_return:
        log("--no-return：不回甩（窗口留在拖到的位置，请自行放回或用一次正常拖动复位）")
        return 0
    # 把窗口放回原位
    ws2 = _windows()
    if ws2:
        back = ws2[0][1] - dx
        post(Quartz.kCGEventLeftMouseDown, ws2[0][1] + 20, ws2[0][2] + 5, DOWN)
        time.sleep(0.05)
        for i in range(1, 11):
            post(Quartz.kCGEventLeftMouseDragged,
                 ws2[0][1] + 20 - dx * i / 10, ws2[0][2] + 5 - dy * i / 10, DOWN)
            time.sleep(0.01)
        post(Quartz.kCGEventLeftMouseUp, ws2[0][1] + 20 - dx, ws2[0][2] + 5 - dy, DOWN)
        log("已把窗口放回原位置附近（起点 x=%.0f）" % back)
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", action="store_true")
    ap.add_argument("--seconds", type=float, default=4.0)
    ap.add_argument("--gap", type=float, default=0.004)
    ap.add_argument("--dx", type=float, default=260.0)
    ap.add_argument("--dy", type=float, default=180.0)
    ap.add_argument("--steps", type=int, default=30)
    ap.add_argument("--hold-ms", type=float, default=20.0)
    ap.add_argument("--no-return", action="store_true",
                    help="只拖一次、不快速回甩：回甩是 2600px/s 的极端手势，"
                         "会把峰值拉高到与正常拖动混在一个数字里")
    a = ap.parse_args()
    if a.sample:
        sample(a.seconds, a.gap)
        return 0
    return drag(a.dx, a.dy, a.steps, a.hold_ms, a.no_return)


if __name__ == "__main__":
    sys.exit(main())
