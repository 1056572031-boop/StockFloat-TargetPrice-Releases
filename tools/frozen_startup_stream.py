# -*- coding: utf-8 -*-
"""tools/frozen_startup_stream.py —— 冻结包冷启动连续取证（人工专项，不进发版链）。

为什么需要它
------------
连续取证的既有证据（outputs/continuous-frames-2026-10-17）只覆盖**源码探针**：
探针在自己进程里构造窗口，能 hook paintEvent、能 patch 网络。台账 A21/C07/D02/D05
挂着"高"，缺的正是**产物层**证据——用户真正拿到的那份 `dist/Skyrocket.app`。
本工具把同一套 Swift 采集流对准冻结包，覆盖两个变体：

  cached    预置缓存冷启动（与源码探针同语义；网络经死代理拒绝，行情来自种子缓存）
  no-cache  无配置无缓存冷启动（默认几何 120,120,MIN_H=224；首价受真实网络支配，
            只记录观测值不判预算；A21 关注的几何跳变在这里查）

判什么（口径与来源）
------------------
- 首价之后的**空窗上界 ≤100ms**：沿用 tools/continuous_frames.py 的同一把尺子
  （phase_report / join_frames），这里不另写一份判据。
- **几何跳变**（A21 签名）：首帧可见之后，价格带定位位置相邻位移 >2px 记一次跳变；
  cached/no-cache 都不允许跳变。窗口改高（先高后扁）会移动价格带 y，逃不过这一条。
- **首价时刻只记录**：冻结包冷启动含进程引导，源码探针的 500ms 预算量的是
  "窗口构造→首价"，不适用。首价晚于 FROZEN_START_EVIDENCE_LIMIT(3s) 视为
  "没起好、证据无效"而拒绝出结论，不冒充产品失败。
- 冻结包版本必须与源码 sf_constants.APP_VERSION 同源，否则拒绝。

硬边界（抄 docs/CONTINUOUS_COMPOSITOR_CHECK.md 并按本工具收窄）
------------------
- 不覆盖全屏/拖动阶段（那是 C07/D02 的下一步，本工具不做）；
- 100ms 源样本间隔内的瞬态空窗不在证明范围；价格像素位置不是玻璃边缘位置；
- 不代表跨机、长期耐久、安装器/OTA 链路；录制自身加负载，资源数字不作数。
- 视频/帧图含取景区桌面内容，**只留本地**： outputs/ 下的本工具产物禁止 git add。

用法
----
    QT_QPA_PLATFORM=offscreen ./.venv/bin/python tools/frozen_startup_stream.py \
        --variant cached --out outputs/frozen-start-<日期>/cached
"""
from __future__ import annotations

import argparse
import json
import os
import plistlib
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

from continuous_frames import (  # noqa: E402
    MAX_BLANK_UPPER,
    MAX_FRAME_GAP,
    ContinuousCapture,
    join_frames,
    phase_report,
)

MARK_STARTUP = "STARTUP v"
MARK_FIRST_FRAME = "first-frame painted"
MARK_UNCAUGHT = "UNCAUGHT"
CLEAN_EXIT_CODES = (0, -15, -2)   # 同 tools/smoke_frozen_app.py：Popen.returncode 用负信号号

APP_REL = Path("dist/Skyrocket.app")
WINDOW_W, WINDOW_H = 184, 224
FROZEN_START_EVIDENCE_LIMIT = 3.0   # 首价超出 = 证据无效（没起好），不是产品判负
GEOMETRY_JUMP_PX = 2
SETTLE_SECONDS = 1.5
TEMPLATE_DOWNSCALE = ("184", "224")

# 种子行情与源码探针（tools/native_glass_probe.py）完全同源：同代码、同价，
# 这样冻结包与源码探针两份证据的渲染输入可比。
SEED_SNAPSHOT = {
    "code": "sh600183", "day": "2026-09-22",
    "quote": {"name": "生益科技", "code": "600183", "price": "119.63",
              "pct": "-2.37", "change": "-2.91", "pre_close": "122.54",
              "high": "126.66", "low": "119.00", "volume": "455539"},
    "minute": [["0930", 122.2, 122.2], ["1030", 124.1, 122.8],
               ["1330", 121.0, 122.8], ["1500", 119.63, 122.63]],
}

VARIANTS: dict[str, dict[str, Any]] = {
    "cached": {
        "window": (400, 180),            # 与源码探针同位，录制区同 [360,140,460,360]
        "recording_rect": [360, 140, 460, 360],
        "config": {"code": "sh600183", "x": 400, "y": 180, "h": WINDOW_H,
                   "bg": "glass", "face_guard": False, "alerts": []},
        "seed_last_quote": True,
        "phase_seconds": 4.0,
    },
    "no-cache": {
        # 无配置 → sf_config.load_config 回落 (120,120,MIN_H=224)（sf_config.py:280）
        "window": (120, 120),
        "recording_rect": [40, 60, 460, 360],
        "config": None,
        "seed_last_quote": False,
        "phase_seconds": 6.0,
    },
}


class TemplateNotReady(RuntimeError):
    """窗口尚无价格前景（占位符帧）—— 可重试，不是证据失败。"""


def variant_plan(variant: str) -> dict[str, Any]:
    if variant not in VARIANTS:
        raise KeyError(variant)
    plan = dict(VARIANTS[variant])
    x, y = plan["window"]
    rx, ry, rw, rh = plan["recording_rect"]
    inside = rx <= x and ry <= y and x + WINDOW_W <= rx + rw and y + WINDOW_H <= ry + rh
    plan["window_inside_rect"] = inside
    return plan


def installed_app_running() -> bool:
    """正式版（/Applications 两代路径）是否在运行。

    只查安装位路径，不查可执行名：本工具自己拉起的 dist 副本同名 Skyrocket，
    按名查会把自己/别的探针误当正式版。反过来，正式版在跑时本工具必须拒绝——
    用户窗口和录制区可能重叠，透出的动态桌面内容会污染判据。
    """
    pattern = ("/Applications/Skyrocket.app/Contents/MacOS/Skyrocket|"
               "/Applications/实时股价-目标价版.app/Contents/MacOS/StockFloatTargetPrice")
    return subprocess.run(["/usr/bin/pgrep", "-f", pattern],
                          capture_output=True).returncode == 0


def app_version(app: Path) -> str:
    info = app / "Contents" / "Info.plist"
    with info.open("rb") as fh:
        return str(plistlib.load(fh).get("CFBundleShortVersionString", ""))


def pid_window_bounds(pid: int) -> list[dict[str, int]]:  # pragma: no cover - real Cocoa capture only
    """按属主 pid 收集屏上窗口 bounds（top-left origin 点坐标，同 screencapture -R）。

    不按 layer 过滤：本产品主窗/玻璃层走置顶设计（实测 layer 25，AGENTS §5.9 同族），
    按"普通窗口=layer 0"的直觉写会一只都找不到 —— 2026-10-17 真跑踩过。
    """
    # pyobjc 无 stubs，仓库既有口径；ignore 注释后不能再跟别的文字（mypy 会判无效）
    import Quartz  # type: ignore[import-untyped]
    options = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
    windows = Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID) or []
    found: list[dict[str, int]] = []
    for item in windows:
        row: Any = item
        if int(row.get("kCGWindowOwnerPID", -1)) != pid:
            continue
        raw: Any = row.get("kCGWindowBounds") or {}
        found.append({key: int(raw.get(key, 0)) for key in ("X", "Y", "Width", "Height")})
    return found


def bounds_close(actual: dict[str, int] | None, expected: tuple[int, int], tolerance: int = 4) -> bool:
    if not actual:
        return False
    x, y = expected
    return (abs(int(actual.get("X", 1 << 30)) - x) <= tolerance
            and abs(int(actual.get("Y", 1 << 30)) - y) <= tolerance
            and abs(int(actual.get("Width", 0)) - WINDOW_W) <= tolerance
            and abs(int(actual.get("Height", 0)) - WINDOW_H) <= tolerance)


def geometry_jumps(frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """首帧可见之后，价格带定位位置相邻位移 >2px 记一次跳变（A21 签名）。

    价格带在 184x224 布局里的偏移是常量 (12,72)，Swift 侧返回的 window_x/y
    已折算回窗口左上角；窗口改高（先高后扁）会移动带 y，位置跳变躲不过。
    """
    jumps: list[dict[str, Any]] = []
    previous: tuple[int, int] | None = None
    for frame in frames:
        if not frame.get("visible"):
            continue
        x, y = frame.get("window_x"), frame.get("window_y")
        if not isinstance(x, int) or not isinstance(y, int):
            continue
        if previous is not None and (abs(x - previous[0]) > GEOMETRY_JUMP_PX
                                     or abs(y - previous[1]) > GEOMETRY_JUMP_PX):
            jumps.append({"time": frame.get("time"), "from": list(previous), "to": [x, y]})
        previous = (x, y)
    return jumps


def _write_plain_png(image, path: Path) -> None:
    """手写最简 PNG（8bit RGBA，无任何辅助块）。

    为什么不走 QImage.save()：screencapture 的产物带 iCCP + cICP 两个色彩块，
    Qt save 会原样拷贝画像；Swift 侧 ImageIO 见到 cICP 就做 P3→sRGB 色域转换，
    饱和绿的红色通道被钳到 0，模板与视频像素红差 82>75 → 202 帧全部判不可见
    （2026-10-17 真跑踩过：去掉 iCCP 不够，cICP 才是元凶）。
    """
    import struct
    import zlib
    w, h = image.width(), image.height()
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        for x in range(w):
            c = image.pixelColor(x, y)
            raw += bytes((c.red(), c.green(), c.blue(), c.alpha()))

    def chunk(ctype: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + ctype + payload
                + struct.pack(">I", zlib.crc32(ctype + payload) & 0xFFFFFFFF))

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
           + chunk(b"IEND", b""))
    path.write_bytes(png)


def template_foreground_ok(png: Path) -> bool:
    """按 Swift PriceMask 同规则自检模板前景/背景量（正>=150 且 负>=150）。"""
    from PyQt6.QtGui import QImage
    image = QImage(str(png))
    if image.width() != WINDOW_W or image.height() < 105:
        return False
    positive = negative = 0
    for y in range(72, 105):
        for x in range(12, 108):
            c = image.pixelColor(x, y)
            mx = max(c.red(), c.green(), c.blue())
            if c.alpha() >= 245 and mx > 170:
                positive += 1
            elif mx < 120:
                negative += 1
    return positive >= 150 and negative >= 150


def capture_template(bounds: dict[str, int], out: Path) -> Path:  # pragma: no cover - real screen capture only
    """截冻结包窗口实拍并落成无画像纯 PNG 模板（184x224 @1x，PriceMask 硬要求）。"""
    rect = "%d,%d,%d,%d" % (bounds["X"], bounds["Y"], bounds["Width"], bounds["Height"])
    raw = out / "window-raw.png"
    template = out / "template-184.png"
    grab = subprocess.run(["/usr/sbin/screencapture", "-x", "-o", "-R", rect, str(raw)],
                          capture_output=True, text=True, timeout=15)
    if grab.returncode or not raw.exists():
        raise RuntimeError("window template screencapture failed: " + grab.stderr[-300:])
    scale = subprocess.run(["/usr/bin/sips", "-z", TEMPLATE_DOWNSCALE[1], TEMPLATE_DOWNSCALE[0],
                            str(raw), "--out", str(template)], capture_output=True, text=True, timeout=15)
    if scale.returncode or not template.exists():
        raise RuntimeError("template downscale failed: " + scale.stderr[-300:])
    probe = subprocess.run(["/usr/bin/sips", "-g", "pixelWidth", "-g", "pixelHeight", str(template)],
                           capture_output=True, text=True, timeout=15)
    if "pixelWidth: 184" not in probe.stdout or "pixelHeight: 224" not in probe.stdout:
        raise RuntimeError("template is not 184x224: " + probe.stdout[-200:])
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtGui import QImage
    image = QImage(str(template))
    if image.width() != WINDOW_W:
        raise RuntimeError("downscaled template unreadable at 184pt width")
    plain = out / "template-plain.png"
    _write_plain_png(image, plain)
    if not template_foreground_ok(plain):
        raise TemplateNotReady("template has insufficient price foreground (placeholder frame?)")
    return plain


def log_anchors(text: str) -> dict[str, Any]:
    lines = [line for line in text.splitlines() if line.strip()]
    return {"startup": any(MARK_STARTUP in line for line in lines),
            "first_frame": any(MARK_FIRST_FRAME in line for line in lines),
            "uncaught": [line for line in lines if MARK_UNCAUGHT in line][:3],
            "lines": len(lines)}


def blank_template(out: Path) -> Path:  # pragma: no cover - exercised by real decode control
    """均匀灰模板：PriceMask 提不出前景，decode 必须拒绝 —— 真数据负向对照用。"""
    from PyQt6.QtGui import QColor, QImage
    image = QImage(184, 224, QImage.Format.Format_ARGB32)
    image.fill(QColor(140, 140, 140, 255))
    path = out / "template-blank.png"
    _write_plain_png(image, path)
    return path


def run(variant: str, out: Path) -> dict[str, Any]:  # pragma: no cover - frozen app/manual evidence only
    from sf_constants import APP_VERSION  # noqa: E402  唯一版本来源

    out.mkdir(parents=True, exist_ok=False)
    plan = variant_plan(variant)
    app = ROOT / APP_REL
    problems: list[str] = []
    if not app.exists():
        raise SystemExit("!! 冻结包不存在：%s（先打包，别拿源码冒充产物）" % app)
    version = app_version(app)
    if version != APP_VERSION:
        raise SystemExit("!! 冻结包版本 %s 与源码 %s 不同源，拒绝出证据" % (version, APP_VERSION))
    if not plan["window_inside_rect"]:
        raise SystemExit("!! 变体窗口不在录制区内，证据不成立")
    if installed_app_running():
        raise SystemExit("!! 检测到 /Applications 正式版在运行；用户窗口可能与录制区重叠，"
                         "请先退出正式版再取证（这不是可跳过的警告）")

    temporary = Path(tempfile.mkdtemp(prefix="sf_frozen_start_"))
    env = dict(os.environ)
    env.pop("QT_QPA_PLATFORM", None)          # 冻结包必须走真 cocoa，不许继承离屏
    for name, filename in {"SF_LOG_PATH": "debug.log", "SF_UNIVERSE_PATH": "securities.json",
                           "SF_SINGLE_SOCK": "single.sock"}.items():
        env[name] = str(temporary / filename)
    if plan["config"] is not None:
        env["SF_CONFIG_PATH"] = str(temporary / "config.json")
        (temporary / "config.json").write_text(json.dumps(plan["config"]), encoding="utf-8")
    if plan["seed_last_quote"]:
        env["SF_LAST_QUOTE_PATH"] = str(temporary / "last_quote.json")
        (temporary / "last_quote.json").write_text(json.dumps(SEED_SNAPSHOT), encoding="utf-8")
        env["SF_SEED_FIRST_SCREEN"] = "1"
    # 与源码探针的 reject_network 同语义：死代理让所有 urllib 出网立刻失败，
    # 行情只来自种子缓存（cached 变体的语义就是"缓存冷启动"，不测网络）。
    env["SF_UNIVERSE_DISABLE"] = "1"
    if variant == "cached":
        dead = "http://127.0.0.1:1"
        env.update({"http_proxy": dead, "https_proxy": dead, "all_proxy": dead})

    capture = ContinuousCapture(out / "continuous", rect=plan["recording_rect"],
                                seconds=plan["phase_seconds"] + 30.0)
    capture.wait_ready(pump=lambda: time.sleep(0.002))
    exe = app / "Contents" / "MacOS" / app.stem
    capture.begin("startup")
    proc = subprocess.Popen([str(exe)], env=env, cwd=str(temporary),
                            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    phase_start = time.monotonic()
    log_path = temporary / "debug.log"
    first_frame_at: float | None = None
    deadline = phase_start + 20.0
    while time.monotonic() < deadline:
        text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
        if MARK_FIRST_FRAME in text:
            first_frame_at = time.monotonic()
            break
        if proc.poll() is not None:
            break
        time.sleep(0.01)
    if first_frame_at is None:
        problems.append("frozen app produced no first-frame anchor before deadline")
    time.sleep(SETTLE_SECONDS)
    candidates = pid_window_bounds(proc.pid)
    bounds = next((b for b in candidates if bounds_close(b, plan["window"])), None)
    if bounds is None:
        problems.append("no pid window matches expected %s; seen=%s" % (plan["window"], candidates))
        template = None
    else:
        # 占位符帧没有价格前景（no-cache 首价受网络支配），按 2s 节奏重试到上限。
        template, retry_deadline = None, time.monotonic() + 12.0
        while template is None:
            try:
                template = capture_template(bounds, out)
            except TemplateNotReady as exc:
                if time.monotonic() >= retry_deadline:
                    problems.append("template never showed price foreground: %s" % exc)
                    break
                time.sleep(2.0)
            except RuntimeError as exc:
                problems.append(str(exc))
                break
    time.sleep(0.5)
    capture.end()
    phase_end = time.monotonic()

    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        problems.append("frozen app ignored SIGTERM")
    if proc.returncode not in CLEAN_EXIT_CODES:
        problems.append("frozen app exit code %r is not clean" % proc.returncode)
    anchors = log_anchors(log_path.read_text(encoding="utf-8", errors="replace")
                          if log_path.exists() else "")
    if not anchors["startup"] or not anchors["first_frame"]:
        problems.append("frozen app log missing STARTUP/first-frame anchors")
    if anchors["uncaught"]:
        problems.append("frozen app logged UNCAUGHT")

    if template is None:
        raise SystemExit("!! 取不到窗口实拍模板，证据链不成立：%s" % "; ".join(problems))
    source, decoded, hashes = capture.finish_evidence(template)
    frames, join_errors = join_frames(source, decoded)
    problems.extend(join_errors)
    report = phase_report(frames, phase_start, phase_end, startup=True,
                          first_visible_budget=FROZEN_START_EVIDENCE_LIMIT)
    # 分析结论必须并进 problems，否则会出现"0 条问题却拒绝"的自相矛盾汇报。
    problems.extend(str(e) for e in report.get("errors", []))
    jumps = geometry_jumps(frames)
    if jumps:
        problems.append("price band moved after first visible frame (A21 geometry jump)")

    # 真数据负向对照：均匀灰模板必须被 PriceMask/decode 拒绝，拿不到"全不可见"的合格分析。
    try:
        decoded_blank = out / "decoded-blank.json"
        blank = blank_template(out)
        proc_blank = subprocess.run([str(capture.binary), "--decode", str(out / "continuous/capture.mov"),
                                     str(blank), str(decoded_blank)],
                                    capture_output=True, text=True, timeout=120)
        negative = {"control": "blank_template_decode",
                    "rejected": proc_blank.returncode != 0}
    except Exception as exc:  # noqa: BLE001  对照失败必须如实记录，不许静默
        negative = {"control": "blank_template_decode", "rejected": False, "error": repr(exc)}
    if not negative.get("rejected"):
        problems.append("blank-template negative control was not rejected")

    passed = not problems and report.get("passed") is True
    result = {
        "variant": variant, "app": str(app), "version": version,
        "passed": passed, "problems": problems,
        "startup_report": report,
        "geometry_jumps": jumps,
        "window_bounds": bounds, "expected_window": list(plan["window"]),
        "first_frame_at": first_frame_at, "phase": {"start": phase_start, "end": phase_end},
        "log_anchors": anchors, "app_exit_code": proc.returncode,
        "recording_rect": plan["recording_rect"], "seeded_last_quote": plan["seed_last_quote"],
        "network": "dead-proxy(cached)" if variant == "cached" else "real(no-cache)",
        "negative_control": negative,
        "evidence_sha256": hashes,
        "limits": [
            "blank/geometry judgements reuse continuous_frames phase_report; no second ruler",
            "first-price timing is recorded, not judged; source-probe 500ms budget is construction-to-price",
            "sub-100ms transients, glass-edge geometry, cross-machine, installer/OTA out of scope",
            "video and frames contain desktop content; local only, never commit or upload",
        ],
    }
    (out / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def self_test() -> int:
    ok = 0

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal ok
        if cond:
            ok += 1
        else:  # pragma: no cover - every shipped self-test assertion is expected to pass
            print("!! 自检失败：%s %s" % (name, detail))
            raise SystemExit(2)

    for variant, expected in (("cached", (400, 180)), ("no-cache", (120, 120))):
        plan = variant_plan(variant)
        check("%s window inside rect" % variant, plan["window_inside_rect"])
        check("%s expected window" % variant, plan["window"] == expected)
    check("no-cache has no config", VARIANTS["no-cache"]["config"] is None)
    check("cached seeds last_quote", VARIANTS["cached"]["seed_last_quote"] is True)

    stable = [{"time": 0.1, "visible": False}] + [
        {"time": 0.2 + i * 0.05, "visible": True, "window_x": 400, "window_y": 180}
        for i in range(10)]
    check("stable layout has no jump", geometry_jumps(stable) == [])
    jitter = [dict(frame, window_x=400 + (1 if i % 2 else 0)) for i, frame in enumerate(stable)]
    check("2px jitter is not a jump", geometry_jumps(jitter) == [])
    moved = [dict(stable[0])] + [
        {"time": 0.25, "visible": True, "window_x": 400, "window_y": 180},
        {"time": 0.30, "visible": True, "window_x": 400, "window_y": 150}]
    check("height jump is caught", len(geometry_jumps(moved)) == 1)
    check("no visible frames is not a crash", geometry_jumps([{"time": 0.1, "visible": False}]) == [])

    check("bounds close accepts truth", bounds_close({"X": 402, "Y": 181, "Width": 184, "Height": 224}, (400, 180)))
    check("bounds close rejects wrong size", not bounds_close({"X": 400, "Y": 180, "Width": 184, "Height": 300}, (400, 180)))
    check("bounds close rejects None", not bounds_close(None, (400, 180)))

    anchors = log_anchors("x\nSTARTUP v1.4.94\nfirst-frame painted\n")
    check("anchors found", anchors["startup"] and anchors["first_frame"] and not anchors["uncaught"])
    check("blank text has no anchors", not log_anchors("")["startup"])

    # 夹具必须满足尺子的前提：15fps 观察下限、首尾覆盖、整数坐标（Swift 解码给 int）。
    def dense(first_delay, end=0.8):
        frames = [{"time": 0.0, "visible": False}]
        t = 0.0
        while t < end - 1e-9:
            t += 0.05
            frames.append({"time": round(t, 3), "visible": t >= first_delay,
                           "window_x": 1, "window_y": 1})
        return frames

    first = phase_report(dense(0.45), 0.0, 0.8, startup=True, first_visible_budget=3.0)
    check("budget override passes slow first price", first["passed"], str(first))
    strict = phase_report(dense(0.45), 0.0, 0.8, startup=True, first_visible_budget=0.2)
    check("budget override still rejects late price", not strict["passed"])

    print("frozen_startup_stream 自检：%d 项通过" % ok)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="frozen_startup_stream.py")
    parser.add_argument("--variant", choices=sorted(VARIANTS), required=False)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--self-test", action="store_true")
    # argv 不含程序名（与 argparse 约定一致）；__main__ 传 sys.argv[1:]。
    args, unknown = parser.parse_known_args(argv)
    if unknown:
        print("!! 不认识参数：%s\n   可用：--variant / --out / --self-test" % " ".join(unknown))
        return 2
    if args.self_test:
        return self_test()
    if args.variant is None or args.out is None:
        parser.print_usage()
        return 2
    result = run(args.variant, args.out)
    print("== 冻结包冷启动取证（%s）：%s ==" % (
        args.variant, "通过" if result["passed"] else "拒绝（%d 条问题）" % len(result["problems"])))
    for problem in result["problems"]:
        print("   · %s" % problem)
    report = result["startup_report"]
    print("   首价观测 %.0fms · 空窗上界 %.0fms · 源样本最大间隔 %.0fms · 几何跳变 %d 次" % (
        (report.get("first_visible_delay") or 0) * 1000,
        (report.get("blank_upper_seconds") or 0) * 1000,
        (report.get("max_source_gap") or 0) * 1000,
        len(result["geometry_jumps"])))
    return 0 if result["passed"] else 1


if __name__ == "__main__":  # pragma: no cover - exercised by direct CLI invocation
    sys.exit(main())
