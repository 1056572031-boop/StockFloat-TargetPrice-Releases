#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第二道防线：真产物冒烟 —— 把打出来的 .app 真的起一次、画一帧、再退出去。

用法：
    ./.venv/bin/python tools/smoke_frozen_app.py dist/Skyrocket.app
    ./.venv/bin/python tools/smoke_frozen_app.py <app> --platform cocoa --timeout 30   # 旧产物用这个
    ./.venv/bin/python tools/smoke_frozen_app.py <app> --self-test

为什么需要它（回归测试全绿也挡不住的那一类）：
    tests/ 跑的是**源码**，产物是 PyInstaller 装配出来的**另一个东西**。
    历史上有一整类缺陷只在产物层出现、源码测试永远看不到：
      · v1.3.74  漏打包 `_cffi_backend` → 已安装程序点「检查更新」中断；
      · v1.4.18  下载完成后退出必崩（iMac SIGSEGV，rc=139）；
      · v1.4.23  临时目录旧副本被重新拉起，在 PyQt6 导入期 SIGSEGV；
      · ≤v1.4.4  打包产物缺 Qt 插件时表现为『双击没反应』，日志一行不写。
    这些的共同点：崩在**进程生命周期**上（起不来 / 退不出去），
    而生命周期恰是源码测试与产物差别最大的地方。

判据（全部来自产物自己写的日志，不看源码）：
    ① 出现 `STARTUP v<版本> run=...`  → 进程真的进了 Python 主流程；
    ② 出现 `first-frame painted WxH`  → 首帧真的画完了（区分"进程活着"与"界面是空的"）；
    ③ 全程不出现 `UNCAUGHT`           → 没有未捕获异常；
    ④ 退出码 0、-15（SIGTERM）或 -2（SIGINT） → 收尾干净；143 是 shell 显示值，不是 Popen.returncode。

安全边界（这条工具会**真的启动一次应用**，所以约束写死）：
    · 所有落盘路径（config/securities/last_quote/log/single sock）重定向到临时目录，
      绝不碰用户的 ~/Library/Application Support/StockFloatTargetPrice；
    · 默认 `offscreen`：不在用户屏幕上弹窗，发版机上跑也不会打断手头工作。
      但**核验 ≤v1.4.32 的旧产物时必须加 `--platform cocoa`**：旧产物在 offscreen
      下几秒内自己 SIGSEGV（实测 rc=-11，源码运行同样复现，日志只 3 行、无 UNCAUGHT）。
      根因是"从 Qt 伸手进 AppKit"时把 offscreen 的 winId() 当成 NSView 指针用，
      已在 sf_glass.content_ns_window / _raise_tool_window 等入口用
      native_cocoa_active() 收口；v1.4.33 复验：offscreen 下存活 10 秒、
      干净退出 rc=-15、并写出 first-frame 锚点。
    · `--platform cocoa` 会短暂出现一个真实悬浮窗，留给"就是要看真窗口"的场合；
    · 不发真实行情请求之外的副作用：更新自检由产品自身的延时定时器控制，
      冒烟窗口通常在其之前就已结束；
    · 只杀**自己拉起的那个 pid**，绝不 pkill 用户正在用的正式版。
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import signal
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import verify_dmg_pyz as VD          # noqa: E402  复用 Info.plist / 可执行文件解析

MARK_STARTUP = "STARTUP v"
MARK_FIRST_FRAME = "first-frame painted"
MARK_UNCAUGHT = "UNCAUGHT"
POST_FRAME_OBSERVATION_SECONDS = 3.0
# 本工具**自己**用 terminate() 收尾，所以"干净退出"只有两种可能：
#   0    产品自己处理了退出并正常返回；
#   -15  被 SIGTERM 直接杀死（Python 的 returncode 用**负信号号**表示）。
# ⚠ 不要写 143：143 是 shell 的显示惯例（128+15），`Popen.returncode` 拿不到它。
# 复核实测（2026-09-24）：Popen 起子进程后 terminate() → wait() 返回 -15。
# 139/-11（SIGSEGV）、137/-9（SIGKILL）一律判失败。
CLEAN_EXIT_CODES = (0, -15, -2)      # -2 = SIGINT：Ctrl-C 收尾同样不算产品缺陷


def exit_code_ok(rc) -> bool:
    """退出码是否算"干净"。单独成函数是为了让它能被**真子进程**验证（见 self_test）。"""
    return rc in CLEAN_EXIT_CODES


def scan_markers(text: str) -> dict:
    """从日志文本里判三件事：起来了没、画帧了没、有没有未捕获异常。"""
    lines = [l for l in text.splitlines() if l.strip()]
    return {
        "startup": [l for l in lines if MARK_STARTUP in l],
        "first_frame": [l for l in lines if MARK_FIRST_FRAME in l],
        "uncaught": [l for l in lines if MARK_UNCAUGHT in l],
        "swallowed": [l for l in lines if "SWALLOWED" in l],
        "lines": len(lines),
    }


def isolation_env(app: pathlib.Path, home: pathlib.Path) -> dict:
    """构造与用户数据完全隔离的运行环境。"""
    env = dict(os.environ)
    env.update({
        "QT_QPA_PLATFORM": env.get("QT_QPA_PLATFORM") or "offscreen",
        "SF_CONFIG_PATH": str(home / "config.json"),
        "SF_UNIVERSE_PATH": str(home / "securities.json"),
        "SF_LAST_QUOTE_PATH": str(home / "last_quote.json"),
        "SF_LOG_PATH": str(home / "debug.log"),
        "SF_SINGLE_SOCK": str(home / "single.sock"),
        "SF_SEED_FIRST_SCREEN": "0",
    })
    # 产物若被拷到临时目录里核验，sf_bootstrap 会按设计直接退出（防旧副本误启动）。
    # 那是核验场景而不是缺陷，所以这里显式放行 —— 且只放行**本工具自己拉的进程**。
    # 判定复用产品侧同一个函数，避免"工具的临时目录口径"和"守卫的口径"长歪。
    sys.path.insert(0, str(ROOT))
    import sf_bootstrap
    if sf_bootstrap.is_temporary_location(str(app)):
        env["SF_ALLOW_TEMP_LAUNCH"] = "1"
    return env


def run_smoke(app: pathlib.Path, platform: str = "offscreen",
              timeout: float = 25.0, keep_dir: bool = False) -> tuple[int, dict]:
    """返回 (退出码, 报告)。0=通过，1=未过，2=工具/环境本身有问题。"""
    app = app.resolve()
    if not app.exists():
        print("!! 产物不存在：%s" % app)
        return 2, {}
    try:
        exe = VD.bundle_executable(app)
    except SystemExit as exc:
        print("!! 读不出可执行文件（Info.plist 有问题？）：%s" % exc)
        return 2, {}
    if not pathlib.Path(exe).exists():
        print("!! 可执行文件不存在：%s" % exe)
        return 2, {}

    home = pathlib.Path(tempfile.mkdtemp(prefix="sf_smoke_"))
    env = isolation_env(app, home)
    env["QT_QPA_PLATFORM"] = platform
    log_path = pathlib.Path(env["SF_LOG_PATH"])
    report = {"app": str(app), "exe": str(exe), "platform": platform,
              "home": str(home), "started": False, "returncode": None}
    proc = None
    try:
        proc = subprocess.Popen([str(exe)], env=env, cwd=str(home),
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.STDOUT)
        report["started"] = True
        deadline = time.time() + timeout
        first_frame_at = None
        markers = scan_markers("")
        while time.time() < deadline:
            text = log_path.read_text(encoding="utf-8", errors="replace") \
                if log_path.exists() else ""
            markers = scan_markers(text)
            if markers["first_frame"] and first_frame_at is None:
                first_frame_at = time.monotonic()
                deadline = max(deadline, time.time() + POST_FRAME_OBSERVATION_SECONDS)
            if proc.poll() is not None:          # 进程提前退出：等再久也不会有首帧
                break
            if (first_frame_at is not None and not markers["uncaught"]
                    and time.monotonic() - first_frame_at >= POST_FRAME_OBSERVATION_SECONDS):
                break
            time.sleep(0.2)
        report.update(markers)
        report["crashed_early"] = proc.poll() is not None
        report["post_frame_seconds"] = (time.monotonic() - first_frame_at
                                        if first_frame_at is not None else 0.0)
        # 收口：只杀自己拉起的这个 pid
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
                report["forced_kill"] = True
        report["returncode"] = proc.returncode
        if log_path.exists():
            report["log_tail"] = log_path.read_text(
                encoding="utf-8", errors="replace").splitlines()[-25:]
        report["last_failures"] = _read_last_failures(home)
    finally:
        if not keep_dir:
            _cleanup(home)
        else:
            print("   保留运行目录：%s" % home)

    return _judge(report), report


def _read_last_failures(home: pathlib.Path):
    """v1.4.33 起的静默失效滚动摘要（没有该文件说明产物早于这个版本）。"""
    f = home / "last_failures.json"
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8")).get("failures")
    except ValueError:
        return {"_parse_error": True}


def _cleanup(home: pathlib.Path) -> None:
    import shutil
    shutil.rmtree(home, ignore_errors=True)


def _judge(report: dict, allow_swallow: bool = False) -> int:
    """判定一次冒烟。`allow_swallow` 只放行"静默失效留痕"这一条，且会打印 WARNING。

    v1.4.59（S7 第三处）：SWALLOWED 与 last_failures.json 过去**只打印不计入 checks**
    —— 产物画完首帧后一路静默失效照样 PASS 放行出包，而这正是防线一/三定义要拦的那类。
    实测基线（本机 1.4.57 装机版，offscreen + 临时 HOME）：SWALLOWED 0 行、
    last_failures 无站点 —— 也就是说这条一开始就是绿的，加它不会挡正常发版。
    """
    swallowed = list(report.get("swallowed") or [])
    sites = sorted(report.get("last_failures") or [])
    checks = [
        ("进程已启动（工具侧）", bool(report.get("started")), ""),
        ("日志出现 STARTUP 行", bool(report.get("startup")),
         "一行都没有 = 进程没进到主流程（缺 Qt 插件/签名被拒/导入期崩溃）"),
        ("日志出现 first-frame painted", bool(report.get("first_frame")),
         "起来了但没画完首帧 = 用户看到的『只有背景/一片空白』"),
        ("首帧后进程持续存活", not report.get("crashed_early"),
         "进程在观察期内自行退出，首帧不能证明启动完成"),
        ("无 UNCAUGHT 未捕获异常", not report.get("uncaught"),
         "; ".join((report.get("uncaught") or [])[:2])),
        ("退出码在干净集合内", exit_code_ok(report.get("returncode")),
         "rc=%s（0 或 -15=SIGTERM 才算干净；-11/139=SIGSEGV、-9/137=SIGKILL 都算退出链炸掉）"
         % report.get("returncode")),
        ("无 SWALLOWED 静默失效留痕", bool(swallowed) is False,
         "本次运行记录到 %d 行 SWALLOWED：%s —— 每一项都是「功能不见了但界面看不出来」"
         % (len(swallowed), "; ".join(x[-90:] for x in swallowed[:2]))),
        ("last_failures.json 无站点", bool(sites) is False,
         "站点：%s（这份摘要只记「哪些功能在哪一刻静默失效过」）" % ", ".join(sites[:6])),
    ]
    if allow_swallow:
        # 显式放行也要在发版日志里留下痕迹，而不是悄悄少一条判据
        checks = [c for c in checks if not c[0].startswith(("无 SWALLOWED", "last_failures"))]
        print("   WARNING 已用 --allow-swallow 跳过静默失效这一条判据："
              "SWALLOWED %d 行 / 站点 %s" % (len(swallowed), ", ".join(sites[:6]) or "无"))
    failed = [c for c in checks if not c[1]]
    print("== 真产物冒烟：%s（platform=%s）==" % (report.get("app"), report.get("platform")))
    for label, ok, detail in checks:
        print("   [%s] %s%s" % ("PASS" if ok else "FAIL", label,
                               "" if ok else "  ← " + detail))
    if report.get("swallowed"):
        print("   提示：本次运行记录到 %d 行 SWALLOWED（功能静默失效留痕）："
              % len(report["swallowed"]))
        for line in report["swallowed"][:5]:
            print("      ", line[-150:])
    if report.get("last_failures"):
        print("   last_failures.json 站点：%s"
              % ", ".join(sorted(report["last_failures"])[:8]))
    print("== 结论:", "冒烟通过 ==" if not failed else "冒烟失败 ==")
    return 1 if failed else 0


def self_test() -> int:
    """保证判定器不是摆设：没有标记时必须判失败，有标记时必须判通过。"""
    bad = []
    empty = scan_markers("")
    if empty["startup"] or empty["first_frame"]:
        bad.append("空日志被扫出了标记 → 判定器在自欺")
    good = scan_markers(
        "[2026-09-24 10:00:00.000 abc123] INFO: STARTUP v1.4.33 run=abc123 | py=3.14.7\n"
        "[2026-09-24 10:00:00.400 abc123] INFO: first-frame painted 184x224\n")
    if not (good["startup"] and good["first_frame"] and not good["uncaught"]):
        bad.append("正常日志没被判成『起来了 + 画完首帧』→ 会把好产物说成坏的")
    crashed = scan_markers(
        "[2026-09-24 10:00:00.000 abc123] ERROR: UNCAUGHT TypeError: boom")
    if not crashed["uncaught"]:
        bad.append("UNCAUGHT 行没被抓到 → 崩溃会被判成通过")
    r1 = _judge({"app": "x", "started": True, "returncode": None})
    if r1 != 1:
        bad.append("缺全部标记的报告被判成通过（rc=%s）→ 闸门永不失败" % r1)
    r2 = _judge({"app": "x", "started": True, "returncode": 139,
                 "startup": ["…"], "first_frame": ["…"], "uncaught": []})
    if r2 != 1:
        bad.append("SIGSEGV 退出码 139 被判成通过 → v1.4.18 那一类又会漏掉")
    r3 = _judge({"app": "x", "started": True, "returncode": 0,
                 "startup": ["…"], "first_frame": ["…"], "uncaught": []})
    if r3 != 0:
        bad.append("干净报告被判成失败 → 闸门会被人绕过")
    # S7 第三处：静默失效留痕必须计入判定（过去只打印，是一道永远过的关）
    dirty = {"app": "x", "started": True, "returncode": 0, "startup": ["…"],
             "first_frame": ["…"], "uncaught": [],
             "swallowed": ["SWALLOWED stock_float_window._raise_tool_window: NameError"]}
    if _judge(dirty) != 1:
        bad.append("有 SWALLOWED 留痕的报告被判成通过 → 冒烟仍然看不见静默失效")
    if _judge(dict(dirty, last_failures=["sf_glass._setup_glass"])) != 1:
        bad.append("last_failures.json 有站点却没判失败 → 摘要白记了")
    if _judge(dirty, allow_swallow=True) != 0:
        bad.append("--allow-swallow 应当放行这一条（同时打印 WARNING），现在仍然红")
    if _judge({"app": "x", "started": True, "returncode": 0, "startup": ["…"],
               "first_frame": ["…"], "uncaught": [], "swallowed": []}) != 0:
        bad.append("空 SWALLOWED 列表被误判成失败 → 每次发版都要人绕闸门")
    # 真子进程验证退出码判据：合成 rc 抓不到"143 还是 -15"这类语义错，
    # 而这类错一旦写错，发版链就是一道**永远红**的关（本仓更怕的是它被绕过）。
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    child.terminate()
    real_rc = child.wait(timeout=10)
    if not exit_code_ok(real_rc):
        bad.append("我们自己 terminate 掉的真子进程返回 rc=%s，却不在干净集合 %s 里 "
                   "→ 发版链每次都会误报失败" % (real_rc, CLEAN_EXIT_CODES))
    if bad:
        print("!! 冒烟工具自检失败：")
        for b in bad:
            print("   -", b)
        return 1
    print("   冒烟判定器自检通过：空日志/崩溃/坏退出码全部判失败，"
          "正常报告与真子进程 SIGTERM 退出判通过")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="启动已打包产物做生命周期冒烟核验")
    ap.add_argument("app", nargs="?", help="待核验的 .app 路径")
    ap.add_argument("--platform", default="offscreen",
                    help="QT_QPA_PLATFORM；默认 offscreen（不在用户屏幕弹窗）。"
                         "核验 ≤v1.4.32 的旧产物时请加 --platform cocoa，理由见 docstring")
    ap.add_argument("--timeout", type=float, default=25.0, help="等首帧的秒数上限")
    ap.add_argument("--keep-dir", action="store_true", help="保留临时运行目录便于排查")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)

    if args.self_test:
        return self_test()
    if not args.app:
        print("!! 需要 .app 路径（或用 --self-test 只验判定器）")
        return 2
    if args.platform == "cocoa":
        print("   提示：屏幕上会短暂出现一个真实悬浮窗（本工具拉起的，跑完自动退出；"
              "与你正在使用的正式版互不影响，落盘路径已全部重定向）")
    rc, _ = run_smoke(pathlib.Path(args.app), args.platform, args.timeout,
                      keep_dir=args.keep_dir)
    return rc


if __name__ == "__main__":
    sys.exit(main())
