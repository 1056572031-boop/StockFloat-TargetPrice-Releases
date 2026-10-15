# -*- coding: utf-8 -*-
"""tools/power_profile.py —— 稳态功耗画像（唤醒次数 / CPU 时间 / 热点分布）。

为什么需要它
------------
「还能不能再省电」这类问题不能靠读代码拍脑袋回答。本工程的轮询链路里有三类
彼此独立的唤醒源：UI 定时器（QTimer）、置顶复核定时器（QTimer）、两条常驻后台
线程（Event.wait 节拍）。macOS 的能耗主要由**唤醒次数**和**每次唤醒干的活**决定，
所以必须先量出「每秒醒多少次、每次烧多少 CPU」，再谈优化。

测什么
------
  wakeups      每秒唤醒次数（UI 轮询 / 置顶复核 / 后台线程节拍 / 动画帧）
  empty_ticks  后台线程节拍里"什么都没拉到"的比例 —— 纯自转，可直接省掉
  cpu_ms/s     进程 CPU 时间 / 墙钟时间，即平均占用的核心比例
  per_thread   主线程与两条后台线程各自的 CPU 秒（time.thread_time 采样）
  hot          cProfile 模式下的函数级热点

用法
----
    # 交易时段（把北京时间注入为 10:30）稳态 45s
    QT_QPA_PLATFORM=offscreen ./.venv/bin/python tools/power_profile.py --mode trading --seconds 45

    # 非交易时段（16:30），同样 45s
    QT_QPA_PLATFORM=offscreen ./.venv/bin/python tools/power_profile.py --mode off --seconds 45

    # 只要热点函数排名
    QT_QPA_PLATFORM=offscreen ./.venv/bin/python tools/power_profile.py --mode trading --seconds 30 --profile

隔离
----
配置、日志、首屏快照、单实例 socket 全部指向临时副本，绝不写用户真实数据
（口径同 sf_constants 的测试隔离；实测一次全量回归就能灌脏 debug.log）。
"""
import argparse
import copy
import datetime
import json
import math
import os
import resource
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
PY = os.path.join(PROJ, ".venv", "bin", "python")

# 注入用的两个"北京时间"锚点：一个是盘中，一个是收盘后。
# 选周二（2026-09-22）避开节假日表，is_holiday 走内置集合、不发请求。
ANCHOR = {
    "trading": datetime.datetime(2026, 9, 22, 10, 30, 0,
                                 tzinfo=datetime.timezone(datetime.timedelta(hours=8))),
    "off": datetime.datetime(2026, 9, 22, 16, 30, 0,
                             tzinfo=datetime.timezone(datetime.timedelta(hours=8))),
}


def prepare_probe_config(directory, source):
    """Copy geometry only; a benchmark must never inherit camera/alert/API settings."""
    config = {}
    try:
        with open(source, encoding="utf-8") as stream:
            stored = json.load(stream)
        if isinstance(stored, dict):
            config = {key: stored[key] for key in ("code", "x", "y", "h", "bg")
                      if key in stored}
    except (OSError, ValueError):
        pass
    config.update(face_guard=False, alerts=[])
    path = os.path.join(directory, "config.json")
    with open(path, "w", encoding="utf-8") as stream:
        json.dump(config, stream)
    return path


def mock_response(url, tick, vary, anchor):
    """Provider-shaped, timestamped data that passes the normal freshness checks."""
    import re
    match = re.search(r"[a-z]{2}\d{6}", url)
    code = match.group(0) if match else "sh600519"
    price, previous = 20 + (tick % 40) * (0.01 if vary else 0), 19.8
    if "web.ifzq.gtimg.cn" in url:
        minutes = list(range(570, 691)) + list(range(781, 901))
        rows = ["%02d%02d %.2f %d %.2f" % (*divmod(minute, 60),
                20 + 0.1 * math.sin(index / 13), (index + 1) * 100,
                (index + 1) * 100 * 100 * 20)
                for index, minute in enumerate(minutes)]
        return json.dumps({"data": {code: {"data": {"date": anchor.date().isoformat(),
                                                   "data": rows}}}})
    if "qt.gtimg.cn" in url:
        fields = [""] * 49
        values = {0: "1", 1: "Benchmark", 2: code[2:], 3: "%.2f" % price,
                  4: "%.2f" % previous, 30: anchor.strftime("%Y%m%d%H%M%S"),
                  31: "%.2f" % (price - previous),
                  32: "%.4f" % ((price - previous) / previous * 100),
                  33: "20.40", 34: "19.80", 36: "1000", 47: "21.78", 48: "17.82"}
        for index, value in values.items():
            fields[index] = value
        return 'v_%s="%s";' % (code, "~".join(fields))
    raise ValueError("Unexpected request in isolated benchmark")


# ---------------------------------------------------------------- 子进程侧
def measure(mode, seconds, net, profile, native=False, vary=False):
    """在当前进程内跑一次稳态测量并打印 JSON（父进程负责 fork 与汇总）。

    native=False 走 offscreen 插件：没有真实 NSWindow，`_force_top` 的 objc 调用
    会直接 SIGSEGV，因此该模式下 `_force_top` 只计次数、不执行本体。
    native=True 用真实窗口跑：能量到置顶复核这一路的真实开销，代价是桌面上
    会短暂出现悬浮窗。
    """
    if not native:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import faulthandler
    faulthandler.enable()          # 离屏插件下的原生崩溃要有栈可查，否则只剩 exit 139
    sys.path.insert(0, PROJ)
    os.chdir(PROJ)

    # ---- 落盘物隔离：必须在 import sf_constants 之前设置 ----
    tmp = tempfile.mkdtemp(prefix="sf_power_")
    real_cfg = os.path.expanduser("~/Library/Application Support/StockFloatTargetPrice/config.json")
    os.environ["SF_CONFIG_PATH"] = prepare_probe_config(tmp, real_cfg)
    os.environ["SF_LAST_QUOTE_PATH"] = os.path.join(tmp, "last_quote.json")
    os.environ["SF_LOG_PATH"] = os.path.join(tmp, "debug.log")
    os.environ["SF_SINGLE_SOCK"] = os.path.join(tmp, "stock_float.sock")
    os.environ["SF_UNIVERSE_PATH"] = os.path.join(tmp, "securities.json")
    os.environ["SF_UNIVERSE_DISABLE"] = "1"
    os.environ["SF_SEED_FIRST_SCREEN"] = "0"

    import stock_float_window as M                       # noqa: E402
    from PyQt6.QtCore import QTimer                      # noqa: E402
    from PyQt6.QtWidgets import QApplication             # noqa: E402
    import sf_quote                                      # noqa: E402

    counts = {}
    spans = {}           # 累计自耗时（ns）
    thread_cpu = {}      # 各后台线程累计 CPU 秒
    last_cpu = {}        # 各线程上一次采样的 thread_time
    lock = threading.Lock()

    def bump(key, ns=0):
        with lock:
            counts[key] = counts.get(key, 0) + 1
            spans[key] = spans.get(key, 0) + ns

    def wrap(owner, name, key):
        """包一层计数 + 自耗时。原方法不存在时静默跳过（不同版本方法名会变）。"""
        orig = getattr(owner, name, None)
        if orig is None:
            return
        def wrapper(*a, **k):
            t0 = time.perf_counter_ns()
            try:
                return orig(*a, **k)
            finally:
                bump(key, time.perf_counter_ns() - t0)
        setattr(owner, name, wrapper)

    # ---- 时钟注入：让"现在几点"固定为盘中 / 盘后 ----
    anchor = ANCHOR[mode]

    def _in_session(a):
        hm = a.hour * 60 + a.minute
        return (9 * 60 + 30 <= hm <= 11 * 60 + 30) or (13 * 60 <= hm <= 15 * 60)

    trading = _in_session(anchor)
    M.market_status = lambda now=None: (None if trading else ("已收盘", M.FG_DIM))

    def _fixed_trading_time(now=None):
        """计数用替身：结果固定，但**调用次数**与真实实现一致，
        便于量出"每秒判几次现在几点"。单次真实耗时另用 microbench 测。"""
        bump("is_trading_time")
        return trading

    M._is_trading_time = _fixed_trading_time

    # ---- 网络：real=走真接口；mock=固定回包（只测调度与绘制，不测网络）----
    if net == "mock":
        tick = [0]
        sf_quote._beijing_now = lambda: anchor
        M._beijing_now = lambda: anchor

        def _fake_http_get(url, **kw):
            result = mock_response(url, tick[0], vary, anchor)
            tick[0] += 1
            return result
        sf_quote.http_get = _fake_http_get

    app = QApplication.instance() or QApplication([])
    M.load_bundled_fonts()

    # ---- 计时/计数钩子必须在 FloatWindow() 之前挂上 ----
    # QTimer.timeout.connect(self._poll) 在 __init__ 里就把**绑定方法**存进了连接表，
    # 之后再改类属性也拦不到那些调用；所以所有 wrap 都要在构造前完成。
    M.start_icon_worker = lambda: None
    for name in ("_setup_reopen_handler", "_show_previous_update_result", "_sync_face_guard", "_prefetch_calendar",
                 "_check_updates_background", "_sweep_temp_copies", "_prune_update_staging"):
        setattr(M.FloatWindow, name, lambda self, *a, **k: None)
    stubbed = ["camera", "notifications(settings cleared)", "updates", "calendar prefetch",
               "icon worker", "reopen handler", "maintenance cleanup"]
    if not native:
        # offscreen 没有真实 NSWindow/NSView，玻璃层与置顶的 objc 调用会直接 SIGSEGV。
        # 退化为"只数次数"：这两路的真实开销改由 --native 模式量。
        def _stub_force_top(self):
            bump("force_top")
        M.FloatWindow._force_top = _stub_force_top
        M.FloatWindow._inject_glass = lambda self: None
        M.FloatWindow.raise_ = lambda self: None
        M.FloatWindow.activateWindow = lambda self: None
        stubbed += ["_force_top(仅计数)", "_inject_glass", "raise_", "activateWindow"]
    else:
        wrap(M.GlassMixin, "_force_top", "force_top")

    # ---- 后台线程节拍采样：每次 _sleep 记一次唤醒。
    #      线程 CPU 用"相邻两次采样的 thread_time 差"累计 —— 一次采样到下一次采样
    #      之间正好覆盖该线程跑完一轮循环的全部 CPU（含 Event.wait 之外的实际工作）。
    orig_sleep = M.FloatWindow._sleep

    def _sleep(self, sec):
        name = threading.current_thread().name
        t = time.thread_time()
        with lock:
            prev = last_cpu.get(name)
            if prev is not None:
                thread_cpu[name] = thread_cpu.get(name, 0.0) + (t - prev)
            last_cpu[name] = t
            counts["thread_tick:" + name] = counts.get("thread_tick:" + name, 0) + 1
        return orig_sleep(self, sec)

    M.FloatWindow._sleep = _sleep

    wrap(M.FloatWindow, "_poll", "ui_poll")
    wrap(M.FloatWindow, "_apply_icons", "apply_icons")
    wrap(M.FloatWindow, "_fetch_quote", "fetch_quote")
    wrap(M.FloatWindow, "_fetch_minute", "fetch_minute")
    wrap(M.FloatWindow, "_anim_tick", "anim_tick")
    wrap(M.FloatWindow, "_auto_width", "auto_width")
    wrap(M.FloatWindow, "_apply_quote", "apply_quote")
    wrap(M.RenderMixin, "paintEvent", "paint")
    wrap(M, "log_line", "log_line")
    wrap(M, "save_config", "save_config")

    # ---- 价格滚动动画：为什么没跑 / 跑了多久（占空比）----
    # 动画由 16ms 的 _anim_timer 驱动，"每秒画几帧"取决于**占空比**：
    # 每条行情滚 100~140ms，行情 10 条/秒时理论上几乎连成一片。
    # 只数 _anim_tick 次数看不出"为什么是 0"，所以把决策链上的每个分支都记下来。
    branches = {}

    def bcount(key):
        branches[key] = branches.get(key, 0) + 1

    orig_susp = M.FloatWindow._animations_suspended

    def _susp(self):
        r = orig_susp(self)
        bcount("anim_suspended" if r else "anim_running")
        return r

    M.FloatWindow._animations_suspended = _susp

    orig_tiny = M.is_tiny_move

    def _tiny(delta_price, delta_pct, tiny_price):
        r = orig_tiny(delta_price, delta_pct, tiny_price)
        bcount("tiny_move" if r else "big_move")
        return r

    M.is_tiny_move = _tiny

    win = M.FloatWindow()
    win.show()

    # 动画启动次数：直接包在 timer 实例上，比推断可靠
    try:
        _orig_timer_start = win._anim_timer.start

        def _ts(*a, **k):
            bcount("anim_timer_start")
            return _orig_timer_start(*a, **k)

        win._anim_timer.start = _ts
    except Exception:
        pass

    anim_samples = [0, 0]          # [总采样次数, 其中"动画正在跑"的次数]

    def _sample_anim():
        anim_samples[0] += 1
        try:
            if win._anim_timer.isActive():
                anim_samples[1] += 1
        except Exception:
            pass

    anim_sampler = QTimer()
    anim_sampler.timeout.connect(_sample_anim)
    anim_sampler.start(20)

    # ---- 重绘走的是整窗还是价格条带？（v1.4.10 补测动画时新增）----
    # v1.2.43 的设计是"动画中间帧只重绘价格条带、分时图每 tick 零重绘"。
    # 但 _apply_quote 每条行情都会把 _need_redraw 置真并走 _poll 的整窗 update()，
    # 两者叠加后到底还有多少帧是局部重绘，只能数出来才知道 —— 这决定了
    # "盘中 CPU 主要花在绘制"这个结论下面还有没有可省的空间。
    upd = {"full": 0, "partial": 0}
    paint_kind = {"full": 0, "partial": 0}
    orig_update = M.FloatWindow.update

    def _update(self, *a):
        upd["partial" if a else "full"] += 1
        return orig_update(self, *a)

    M.FloatWindow.update = _update

    orig_paint = M.RenderMixin.paintEvent
    from sf_ui_render import is_price_partial_redraw

    def _paint(self, e):
        # 分类必须按"本次绘制事件的真实损坏区"判定，不能只看 _partial_price 是否置位：
        # v1.4.15 把同频重复帧合并到点阵时钟后，动画帧可能被跳过，脏区会悬挂到
        # 下一次整窗重绘上 —— 按标志位分类会把整窗帧误记成局部帧（实测把 60/秒
        # 误报成 7.2/秒）。这里与 paintEvent 生产路由用同一个判据。
        partial = getattr(self, "_partial_price", None)
        r = e.rect()
        hit = bool(partial) and is_price_partial_redraw(
            (r.x(), r.y(), r.width(), r.height()), partial)
        paint_kind["partial" if hit else "full"] += 1
        return orig_paint(self, e)

    M.RenderMixin.paintEvent = _paint

    # 计时起点：窗口已经建好、首屏已落定，测的是"稳态"而非冷启动
    cpu0 = resource.getrusage(resource.RUSAGE_SELF)
    wall0 = time.monotonic()

    def finish():
        """采样收口并退出。

        不能走 app.quit()：真实窗口模式下 quit 会进入 FloatWindow 的退出流程，
        那条路径末尾自己就 os._exit(0)，测量结果会被吞掉（实测 exec() 之后
        的代码根本不执行）。所以这里直接由定时器调本函数，采完即退。
        """
        wall = time.monotonic() - wall0
        cpu1 = resource.getrusage(resource.RUSAGE_SELF)
        cpu_s = (cpu1.ru_utime + cpu1.ru_stime) - (cpu0.ru_utime + cpu0.ru_stime)
        out = {
            "mode": mode,
            "net": net,
            "vary": vary,
            "native": native,
            "seconds": round(wall, 2),
            "cpu_seconds": round(cpu_s, 3),
            "cpu_percent_of_core": round(cpu_s / wall * 100.0, 2) if wall else None,
            "wakeups_per_second": {k: round(v / wall, 1) for k, v in counts.items()},
            "call_counts": dict(counts),
            "self_ms_total": {k: round(v / 1e6, 2) for k, v in spans.items()},
            "self_us_per_call": {k: round(spans[k] / counts[k] / 1000.0, 1)
                                 for k in counts if k in spans and counts[k]},
            "thread_cpu_seconds": {k: round(v, 4) for k, v in thread_cpu.items()},
            "poll_state": win._poll_state,
            "poll_fail": win._poll_fail,
            "minute_points": len(win.minute_points),
            "has_quote": bool(win.quote),
            "window_visible": win.isVisible(),
            "first_frame_painted": bool(win._sf_first_frame_logged),
            "timing_semantics": "inclusive wall time per wrapped call; nested totals must not be added",
            "net_snapshot": None,
            "stubbed": stubbed,
            "anim_branches": dict(branches),
            "update_calls": dict(upd),
            "paint_kind": dict(paint_kind),
            "anim_duty_cycle_pct": round(
                (anim_samples[1] / anim_samples[0] * 100.0) if anim_samples[0] else 0.0, 1),
            "anim_timer_interval_ms": (win._anim_timer.interval()
                                       if win._anim_timer.isActive() else None),
            "tmp": tmp,
        }
        valid = net != "mock" or (bool(win.quote) and bool(win.minute_points)
                                    and win._poll_fail == 0 and win._poll_state == 0
                                    and out["window_visible"] and out["first_frame_painted"])
        if native:
            screenshot = os.path.join(tmp, "native-frame.png")
            try:
                if not win.grab().save(screenshot):
                    raise OSError("Native frame could not be saved")
                out["native_frame"] = screenshot
            except Exception as exc:
                out["native_frame_error"] = repr(exc)
                valid = False
        out["probe_valid"] = valid
        try:
            from sf_network import _NET_MONITOR
            out["net_snapshot"] = _NET_MONITOR.snapshot()
        except Exception:
            pass
        if profile:
            prof.disable()
            prof.dump_stats(os.path.join(tmp, "power.prof"))
            buf = io.StringIO()
            pstats.Stats(prof, stream=buf).sort_stats("tottime").print_stats(30)
            out["profile_text"] = buf.getvalue()
        print(json.dumps(out, ensure_ascii=False, indent=2))
        sys.stdout.flush()
        # 离屏插件下窗口析构会 SIGSEGV（见 coldstart_bench 同注释），测量已完成，直接退出
        os._exit(0 if valid else 2)

    if profile:
        import cProfile
        import pstats
        import io
        prof = cProfile.Profile()
        prof.enable()
    else:
        prof = None
    QTimer.singleShot(int(seconds * 1000), finish)
    app.exec()
    finish()      # exec() 若被外部提前返回，也要把结果收出来


# ---------------------------------------------------------------- 父进程侧
def run_child(args):
    cmd = [PY, os.path.abspath(__file__), "--child"]
    cmd += ["--mode", args.mode, "--seconds", str(args.seconds), "--net", args.net]
    if args.vary:
        cmd += ["--vary"]
    if args.profile:
        cmd += ["--profile"]
    if args.native:
        cmd += ["--native"]
    env = dict(os.environ)
    if not args.native:
        env["QT_QPA_PLATFORM"] = "offscreen"
    else:
        env.pop("QT_QPA_PLATFORM", None)
    p = subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=PROJ)
    if p.returncode != 0:
        print("测量子进程失败 exit=%d\n%s" % (p.returncode, p.stderr), file=sys.stderr)
        return None
    if not p.stdout.strip():
        print(p.stderr, file=sys.stderr)
        return None
    return json.loads(p.stdout)


def brief(d):
    w = d["wakeups_per_second"]
    print("\n[%s / net=%s] 稳态 %ss，CPU %.2f%% 单核（%.3fs）" % (
        d["mode"], d["net"], d["seconds"], d["cpu_percent_of_core"], d["cpu_seconds"]))
    order = ["ui_poll", "force_top", "anim_tick", "paint", "apply_quote",
             "apply_icons", "fetch_quote", "fetch_minute", "is_trading_time",
             "log_line"]
    for k in order:
        if k in w:
            print("   %-16s %7.1f 次/秒   单次 %7.1f µs   合计 %6.1f ms" % (
                k, w[k], d["self_us_per_call"].get(k, 0.0),
                d["self_ms_total"].get(k, 0.0)))
    ticks = {k: v for k, v in d["call_counts"].items() if k.startswith("thread_tick:")}
    tcpu = d["thread_cpu_seconds"]
    for k in sorted(ticks):
        name = k.split(":", 1)[1]
        per = ticks[k] / d["seconds"]
        key = "fetch_minute" if "minute" in name else "fetch_quote"
        fetches = d["call_counts"].get(key, 0)
        empty = max(ticks[k] - fetches, 0) / ticks[k] * 100 if ticks[k] else 0
        print("   %-16s %7.1f 次/秒   线程 CPU %8.4fs   空转占比 %.1f%%" % (
            "tick:" + name, per, tcpu.get(name, 0.0), empty))
    ab = d.get("anim_branches") or {}
    if ab or d.get("anim_duty_cycle_pct") is not None:
        print("   动画决策链: " + ("  ".join("%s=%d" % kv for kv in sorted(ab.items())) or "(无)"))
        print("   动画占空比: %.1f%%   （_anim_timer 正在跑的采样占比，间隔 %s ms）" % (
            d.get("anim_duty_cycle_pct") or 0.0,
            d.get("anim_timer_interval_ms") or "未激活"))
        uc, pk = d.get("update_calls") or {}, d.get("paint_kind") or {}
        if uc or pk:
            tot_u = sum(uc.values()) or 1
            tot_p = sum(pk.values()) or 1
            print("   update() 调用: 整窗 %d (%.0f%%) / 局部 %d (%.0f%%)" % (
                uc.get("full", 0), uc.get("full", 0) * 100.0 / tot_u,
                uc.get("partial", 0), uc.get("partial", 0) * 100.0 / tot_u))
            print("   实际绘制:     整窗 %d (%.0f%%) / 局部 %d (%.0f%%)" % (
                pk.get("full", 0), pk.get("full", 0) * 100.0 / tot_p,
                pk.get("partial", 0), pk.get("partial", 0) * 100.0 / tot_p))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["trading", "off"], default="trading")
    ap.add_argument("--seconds", type=float, default=30.0)
    ap.add_argument("--net", choices=["real", "mock"], default="real")
    ap.add_argument("--vary", action="store_true",
                    help="mock 回包逐次变价，逼出稳态重绘（真实盘口就是这种形态）")
    ap.add_argument("--profile", action="store_true")
    ap.add_argument("--native", action="store_true",
                    help="用真实窗口跑（桌面上会短暂出现悬浮窗），可量到置顶复核的真实开销")
    ap.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--json", action="store_true", help="父进程只输出原始 JSON")
    args = ap.parse_args()

    if args.child:
        measure(args.mode, args.seconds, args.net, args.profile,
                native=args.native, vary=args.vary)
        return

    d = run_child(args)
    if d is None:
        print("子进程未返回数据", file=sys.stderr)
        sys.exit(1)
    if args.json:
        print(json.dumps(d, ensure_ascii=False, indent=2))
    else:
        brief(d)
        print("\n  原始 JSON 已写入 /tmp/sf_power_last.json")
        with open("/tmp/sf_power_last.json", "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
