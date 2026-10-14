# -*- coding: utf-8 -*-
"""模块化拆分 v1.2.13：sf_logging —— 由 stock_float_window.py 搬移生成，行为零变化。"""

import datetime
import errno
import json
import os
import re
import socket
import stat
import sys
import threading
import time
from typing import Any
from sf_privacy import scrub_secrets
from sf_instance import acquire_instance_lock
from PyQt6.QtCore import QTimer
from sf_constants import (APP_DISPLAY_NAME, APP_VERSION, LEGACY_EXECUTABLE_NAMES,
                          LOG_MAX_AGE_DAYS, LOG_MAX_BYTES, LOG_PATH,
                          LOG_ROTATE_CHECK_INTERVAL, LOG_ROTATE_KEEP,
                          SINGLE_SOCK, _FEATURE_DEFAULTS)

# 崩溃记录文件名前缀：两代可执行名都认，唯一来源是 sf_constants（v1.4.64）。
# 与 sf_crashreport.CRASH_NAME_PREFIXES 同值；不跨模块互相引用是为了避免
# 日志层依赖取证层（sf_crashreport 反过来要经 sf_logging 写日志）。
CRASH_PREFIXES = (APP_DISPLAY_NAME, *LEGACY_EXECUTABLE_NAMES)

def _load_feature_flags():
    """读取环境变量 STOCKFLOAT_FEATURES（逗号分隔）或同目录 features.ini，
    解析每项功能开关；未配置项缺省开启。任何解析失败都回退默认，不抛异常。
    语法：`name` 开启；`!name` 或 `name=0/off/false` 关闭。"""
    flags = dict(_FEATURE_DEFAULTS)
    try:
        def _apply(tokens):
            for raw in tokens:
                p = raw.strip()
                if not p:
                    continue
                neg = False
                if p.startswith("!"):
                    neg = True
                    p = p[1:]
                elif "=" in p:
                    p, val = p.split("=", 1)
                    neg = val.strip().lower() in ("0", "off", "false", "no")
                name = p.strip().lower()
                if name in flags:
                    flags[name] = not neg
        # 环境变量与 features.ini 是二选一而非叠加：打包后的 .app 内改 ini 很麻烦，
        # 环境变量留了一条不动安装目录就能临时开关功能的覆盖通道。
        env = os.environ.get("STOCKFLOAT_FEATURES", "").strip()
        if env:
            _apply(env.replace(",", " ").split())
        else:
            ini = os.path.join(os.path.dirname(os.path.abspath(__file__)), "features.ini")
            if os.path.isfile(ini):
                with open(ini, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.split("#", 1)[0].strip()
                        if not line or "=" not in line:
                            continue
                        k, v = line.split("=", 1)
                        k = k.strip().lower()
                        if k in flags:
                            flags[k] = v.strip().lower() in ("1", "true", "yes", "on")
    except Exception:
        pass
    return flags

_FEATURES = _load_feature_flags()

def feature_enabled(name):
    """按开关名查询功能是否开启；未知开关一律按默认处理（不抛异常）"""
    try:
        return bool(_FEATURES.get(name, _FEATURE_DEFAULTS.get(name, False)))
    # 兜底 True：开关表损坏时按"功能开启"处理 —— 返回 False 会静默关掉功能，
    # 用户看到的是功能凭空消失，比留下一条可见的错误更难排查。
    except Exception:
        return True

def feature_snapshot():
    """诊断导出用：每个功能开关**此刻的实际生效值**。

    为什么必须有（1.4.37 那一族的现场）：`system_drag` 这类开关此前只活在
    STARTUP 行的 `features=…` 日志里，被 5MB×3 轮转挤掉就等于这台机器到底
    走的哪条拖拽路线再也问不出来 —— 而"拖动时背景慢半拍"的定位第一步就是它。
    """
    try:
        return dict(_FEATURES)
    except Exception:
        return {}

def _unlink_stale_single_socket(path):
    """仅删除已确认没有监听者的 Unix socket；其他路径类型与异常绝不触碰。"""
    # 用 lstat 而非 stat：符号链接本身不是 socket，跟随链接会把判断打到它指向的
    # 目标上，进而把一个完全无关的文件当成陈旧 endpoint 删掉。
    try:
        if not stat.S_ISSOCK(os.lstat(path).st_mode):
            log_line("single-instance path is not a socket: %s" % path, "WARNING")
            return False
        os.unlink(path)
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        log_line("single-instance stale socket cleanup skipped: %r" % (exc,), "WARNING")
        return False


_INSTANCE_LEASES: list = []


def _become_single_instance():
    """文件锁决定唯一性，socket 只兼容仍在运行的旧版探活。"""
    try:
        lease = acquire_instance_lock(SINGLE_SOCK + ".lock")
    except OSError as exc:
        log_line("single-instance lock unavailable; startup refused: %r" % (exc,), "ERROR")
        return False
    if lease is None:
        return False
    try:
        primary = _open_single_listener()
    except Exception:
        lease.close()
        raise
    if primary:
        _INSTANCE_LEASES.append(lease)
    else:
        lease.close()
    return primary


def _open_single_listener():
    """多开保护：当前进程成为唯一实例时返回 True；已有实例在运行则返回 False。
    仅在连接被拒绝且路径确认是 Unix socket 时清理陈旧 endpoint，避免启动竞态误删。"""
    try:
        os.makedirs(os.path.dirname(SINGLE_SOCK), exist_ok=True)
    except Exception:
        _note_internal_failure("single_instance_mkdir")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale = False
    try:
        s.settimeout(0.5)
        s.connect(SINGLE_SOCK)
        return False  # 已有实例
    except FileNotFoundError:
        stale = False  # 路径不存在，无需清理
    except ConnectionRefusedError:
        stale = True  # 无监听者；仍须确认路径类型后才可清理
    except OSError as exc:
        # 权限、路径错误、超时等均不应被误判成陈旧 socket。
        log_line("single-instance probe failed without cleanup: %r" % (exc,), "WARNING")
    finally:
        try:
            s.close()
        except OSError:
            _note_internal_failure("single_instance_probe_close")
    if stale:
        _unlink_stale_single_socket(SINGLE_SOCK)
    try:
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(SINGLE_SOCK)
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:
                try:
                    live_socket = stat.S_ISSOCK(os.lstat(SINGLE_SOCK).st_mode)
                except OSError:
                    live_socket = False
                server.close()
                return False if live_socket else True
            raise
        try:
            # 18.1 安全加固：socket 文件仅当前用户可读写（默认 umask 可能放开读权限）
            os.chmod(SINGLE_SOCK, 0o600)
        except Exception:
            _note_internal_failure("single_instance_chmod")
        server.listen(1)
        # v1.2.95 第3轮走查：改为**阻塞 accept**，去掉常驻轮询。
        # 原实现 settimeout(0.5) + except + sleep(0.2)，让这条线程约 1.4 次/秒
        # 被唤醒，贯穿整个进程生命周期 —— 本应用是常驻悬浮窗，等于全天制造
        # 十几万次无用唤醒，与 App Nap 省电目标直接相悖。
        # accept 本身就是"新实例来探活"的唯一事件，本就无需轮询。
        def _keep():
            idle = 0
            while True:
                try:
                    conn, _ = server.accept()
                    conn.close()
                    idle = 0
                except Exception:
                    # 仅 socket 失效（进程退出/被删）时异常；连续失败即放弃多开
                    # 保护，绝不留一个空转的循环。
                    idle += 1
                    if idle > 3:
                        return
                    time.sleep(1.0)
        threading.Thread(target=_keep, daemon=True).start()
        return True
    except Exception:
        # 无法创建（权限/残留冲突等）也不拦截，避免误拦正常启动。
        # v1.4.18 建议#7：bind/listen 抛错时把已创建的 socket 关掉再放行，
        # 否则每触发一次失败路径就漏一个 FD。server 可能尚未赋值（socket()
        # 本身失败），用 try 兜住 NameError/OSError。
        try:
            server.close()
        except Exception:
            pass
        return True

# v1.2.71 第4轮走查：轮转与写入统一持锁。原实现每次 log_line 都无锁做
# `stat + os.replace + listdir`，多线程（行情线程 / 分时线程 / 主线程）并发
# 写日志时，两条线程可能同时判定"超限"并各自 replace，其中一次会把另一条
# 刚写下的内容整段丢掉；listdir 清理与 replace 之间也存在窗口。
_LOG_LOCK = threading.Lock()
# 轮转检查节流状态（单调时钟秒，见 sf_constants.LOG_ROTATE_CHECK_INTERVAL）
_LAST_ROTATE_CHECK = 0.0


def _shift_log_generations(path, keep):
    """把 debug.log → .1、.1 → .2 … 依次后移，超出 keep 的最老一代丢弃。

    原实现只有一代（.1）：日志写满一次就把上一代整段覆盖，若这次写满正是
    故障刷屏造成的，则故障**之前**的日志全部丢失，排障只剩刷屏内容。
    """
    oldest = "%s.%d" % (path, keep)
    if os.path.exists(oldest):
        try:
            os.remove(oldest)
        except Exception:
            pass
    # 必须从最老一代往新代倒序搬迁：正序会先把 .1 覆盖到 .2，再拿已被覆盖的
    # .2 去覆盖 .3，整条历史链被同一次内容污染，等于只剩一份副本。
    for i in range(keep - 1, 0, -1):
        src = "%s.%d" % (path, i)
        if os.path.exists(src):
            try:
                dst = "%s.%d" % (path, i + 1)
                os.replace(src, dst)
                os.chmod(dst, 0o600)
            except Exception:
                pass
    try:
        os.replace(path, path + ".1")
        os.chmod(path + ".1", 0o600)
    except Exception:
        pass


def _maybe_rotate_log():
    """debug.log 超限后滚动为 debug.log.1，防止无限增长；并清理超过
    LOG_MAX_AGE_DAYS 天的历史轮转旧档（11.1 按时间清理）。

    v1.2.71：① 由 log_line 持 _LOG_LOCK 调用，消除并发轮转丢日志；
    ② 保留 LOG_ROTATE_KEEP 代而非单代；③ 检查按 LOG_ROTATE_CHECK_INTERVAL
    节流 —— 原实现每条日志都做一次 stat + listdir，高频日志路径下纯属浪费。"""
    global _LAST_ROTATE_CHECK
    try:
        now = time.monotonic()
        if now - _LAST_ROTATE_CHECK < LOG_ROTATE_CHECK_INTERVAL:
            return
        _LAST_ROTATE_CHECK = now
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > LOG_MAX_BYTES:
            _shift_log_generations(LOG_PATH, LOG_ROTATE_KEEP)
        # 11.1：删除超过 N 天的轮转旧档（debug.log.1 / debug.log.2 ...）
        cutoff = time.time() - LOG_MAX_AGE_DAYS * 86400
        base_dir = os.path.dirname(LOG_PATH)
        prefix = os.path.basename(LOG_PATH) + "."
        # 只认 "<日志名>." 前缀的轮转档：日志目录里还放着配置等其它文件，
        # 无前缀过滤会把 listdir 到的任何文件都拿去按 mtime 删。
        for name in os.listdir(base_dir):
            if not name.startswith(prefix):
                continue
            p = os.path.join(base_dir, name)
            try:
                if os.path.getmtime(p) < cutoff:
                    os.remove(p)
            except Exception:
                pass
    except Exception:
        pass

def _nslog_line(line):
    """把一行文本经 NSLog 写入 macOS 统一日志（19.1），尽力而为不抛异常。
    优先 pyobjc，缺失时回退 ctypes 构造 NSString。"""
    try:
        try:
            from Foundation import NSLog, NSString
            NSLog("%@", NSString.alloc().initWithUTF8String_(line.encode("utf-8")))
            return
        except Exception:
            pass
        try:
            import ctypes
            libobjc = ctypes.CDLL("/usr/lib/libobjc.A.dylib")
            libobjc.objc_getClass.restype = ctypes.c_void_p
            libobjc.sel_registerName.restype = ctypes.c_void_p
            libobjc.sel_registerName.argtypes = [ctypes.c_char_p]
            libobjc.objc_msgSend.restype = ctypes.c_void_p
            libobjc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
            NSString_cls = libobjc.objc_getClass(b"NSString")
            sel_alloc = libobjc.sel_registerName(b"alloc")
            sel_init = libobjc.sel_registerName(b"initWithUTF8String:")
            ns = libobjc.objc_msgSend(NSString_cls, sel_alloc)
            s = libobjc.objc_msgSend(ns, sel_init, line.encode("utf-8"))
            libf = ctypes.CDLL("/System/Library/Frameworks/Foundation.framework/Foundation")
            # v1.2.65 第1轮走查修复：NSLog 的**第一个参数是格式串**，日志原文必须
            # 作为实参经 "%@" 传入。旧实现把日志原文直接当格式串，而本项目日志里
            # 普遍含 '%'（如 "monitor rate=100.0%"、"paintEvent error: %r" 展开后的
            # 文本），NSLog 会把 '%' 当格式说明符去读不存在的可变参数 → 读栈垃圾、
            # 输出乱码，最坏情况进程崩溃（ctypes 调用引发的崩溃无法被 except 捕获）。
            # 此处与上方 pyobjc 主路径的 NSLog("%@", ...) 语义对齐。
            sel_fmt = libobjc.sel_registerName(b"stringWithUTF8String:")
            fmt = libobjc.objc_msgSend(NSString_cls, sel_fmt, b"%@")
            libf.NSLog.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            libf.NSLog.restype = None
            libf.NSLog(fmt, s)
        except Exception:
            pass
    except Exception:
        pass

# v1.3.39：日志静音开关。卸载流程把数据目录移入废纸篓后，任何一次 log_line
# 都会把目录和 debug.log 重新建出来 —— 表现为"刚清完又冒出一个空日志"。
# 卸载执行期间必须静音，保证清得干净。
_LOG_MUTED = False


def set_log_muted(v=True):
    """关闭/恢复日志文件写入（卸载清理期间使用）。"""
    global _LOG_MUTED
    _LOG_MUTED = bool(v)


def is_log_muted():
    """当前是否处于静音（供崩溃钩子等**绕开 log_line** 的写盘路径查询）。

    v1.3.42 第5轮走查：sys.excepthook 直接 open(LOG_PATH, "a") 写文件、不经过
    log_line，于是 set_log_muted(True) 对它完全无效 —— 卸载流程刚把
    ~/Library/Application Support 移进废纸篓，一次未捕获异常就把目录和
    debug.log 重新建了出来（"清完又冒出来"）。任何自行写日志文件的路径
    都必须先问一次这里。
    """
    return _LOG_MUTED


# 日志子系统**自己**出问题的计数（诊断用，不写日志：日志都写不动了，
# 再套一层 log_swallow 就是自我递归）。这些站点刻意不新增"纯 except: pass"，
# 因为仓库规矩是"兜异常不许静默"，计数就是它们留下的痕。
_INTERNAL_FAULTS: dict = {}


def _note_internal_failure(name):
    try:
        _INTERNAL_FAULTS[name] = _INTERNAL_FAULTS.get(name, 0) + 1
    except Exception:
        # 给字典自增这件事再兜一层已无可作为；此处唯一可能的失败是解释器
        # 正在关闭（KeyboardInterrupt 后的 shutdown 期），此时计数已无意义。
        pass


def internal_faults():
    """返回日志内部失败计数的快照（诊断导出可读它）。"""
    return dict(_INTERNAL_FAULTS)


# ---- v1.4.33 定位能力：每条日志带完整日期 + 本次进程的 run id ----
# 为什么改：原格式只有 `[时:分:秒]`。debug.log 是 5MB×3 代轮转、7 天清理，
# 用户回传的诊断包里跨天记录混在一起，**根本判不出是哪天发生的**；一次崩溃重启
# 之后新旧两次运行的行首尾相接，也没法把"用户做的那一步"串成因果链。
# run id 取进程启动时的 24bit 随机数（6 个十六进制字符），只用于区分同文件里的
# 不同次运行，不承担唯一性职责。
RUN_ID = "%06x" % int.from_bytes(os.urandom(3), "big")


def log_ts(now=None):
    """一行日志的时间前缀：'YYYY-MM-DD 时:分:秒.毫秒 runid'。

    单独成函数是为了让"格式"只有一处定义（NSLog 分支与文件分支必须同源）。
    """
    now = now or datetime.datetime.now()
    return "%s.%03d %s" % (now.strftime("%Y-%m-%d %H:%M:%S"),
                           now.microsecond // 1000, RUN_ID)


def log_line(msg, level="INFO"):
    """追加一行带级别的调试日志（超限自动轮转），不抛异常。
    WARNING/ERROR 级别额外经 NSLog 写入 macOS 统一日志（19.1），便于用
    `log show --last 1h --predicate 'process == "Skyrocket"'` 排查崩溃。
    （进程名 = 可执行文件名 = sf_constants.APP_DISPLAY_NAME；v1.4.55 改名前它叫
    StockFloatTargetPrice，而这句话一直写着中文显示名"实时股价" —— 照抄的人
    查不到任何记录，会以为统一日志里没有崩溃。判据 tests/test_identity_literals.py。）

    v1.2.71 第4轮走查：① 轮转与写入合并在 _LOG_LOCK 内，避免并发轮转丢日志；
    ② 显式 encoding="utf-8" —— 打包成 .app 从访达启动时环境变量 LANG 常未设置，
    缺省编码不一定是本机的 UTF-8，日志里的中文（股票名/错误文案）会直接
    UnicodeEncodeError，而这行异常又在 except 内被吞掉，表现为"日志莫名少一截"；
    ③ errors="replace"：行情源偶发返回含代理项/surrogate 的脏串时不会连带
    丢掉整条日志。"""
    if _LOG_MUTED:
        return
    # 行文本只拼一次：文件与 NSLog 必须同源，否则同一行在两边时间戳不同，
    # 用 `log show` 对照 debug.log 时会对不上。
    line = "[%s] %s: %s" % (log_ts(), level, scrub_secrets(msg))
    try:
        with _LOG_LOCK:
            _maybe_rotate_log()
            try:
                os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
                with open(LOG_PATH, "a", encoding="utf-8", errors="replace") as f:
                    f.write(line + "\n")
                os.chmod(LOG_PATH, 0o600)
            except Exception:
                _note_internal_failure("file_write")
        # 只有 WARNING/ERROR 进系统日志：统一日志是全局共享的，全量 INFO 会淹没
        # 其它进程，且 NSLog 单次调用远比追加写文件贵，冷路径才值得付这个代价。
        if level in ("WARNING", "ERROR"):
            _nslog_line(line)
    except Exception:
        _note_internal_failure("log_line")

# ---- v1.4.33 启动锚点：一行回答"这次开起来的到底是什么" --------------------
def log_startup_banner(extra=None):
    """启动时写一条身份行：版本 / run id / 是否冻结产物 / Qt 平台 / 生效的功能开关。

    为什么必须有这条（2026-09-24 实测的三处空白）：
      · 功能开关 STOCKFLOAT_FEATURES / features.ini 有 11 个读取点，却**从来没落过
        日志** —— 用户报"某功能不见了"，第一件要排除的事查不到；
      · 崩溃报告与诊断包都要能回答"这是哪一次启动、哪个版本、cocoa 还是 offscreen"，
        此前只能从可执行文件路径反推；
      · v1.4.20 / v1.4.23 两次 iMac 崩溃都靠"产物起不来"定位，而日志里连一条
        启动行都没有，无法区分"没起来"与"起来了但界面是空的"。
    成本：一次文件追加（微秒级）。调用位置在 excepthook 装好之后、QApplication
    构造之前，让最早的失败也带着身份。绝不抛异常。
    """
    try:
        feats = ",".join("%s=%d" % (k, 1 if v else 0)
                         for k, v in sorted(_FEATURES.items()))
        parts = ["STARTUP v%s run=%s" % (APP_VERSION, RUN_ID),
                 "py=%d.%d.%d" % sys.version_info[:3],
                 "frozen=%d" % (1 if getattr(sys, "frozen", False) else 0),
                 "qt=%s" % (os.environ.get("QT_QPA_PLATFORM") or "default"),
                 "exe=%s" % getattr(sys, "executable", "?"),
                 "features=%s" % (feats or "none")]
        if extra:
            parts.append(extra)
        log_line(" | ".join(parts))
        return True
    except Exception as exc:
        # 唯一的写盘通道就是日志本身，这里没有第二个地方可以报。
        # 失败的可观测形式就是"日志里没有 STARTUP 行"——产物冒烟关卡正以此为断言。
        try:
            print("[sf_logging] startup banner failed: %r" % (exc,), file=sys.stderr)
        except Exception:
            _note_internal_failure("startup_banner_stderr")
        _note_internal_failure("startup_banner")
        return False


# v1.2.95 第3轮走查：高频重复日志节流。
# 日志是单文件 5MB × 3 代轮转、无级别分流也无去重，任何处在 10~60Hz 路径上
# 的异常（行情轮询 / 绘制 / 动画）都会以每秒十条计地写入，几小时就把真正有用
# 的故障记录挤出轮转窗口 —— 实测仅 autowidth 一项就占过 12.3 万条。
# 通用策略：同一 key 首条必记（保证故障第一时间可见），其后每 every 条记一次
# 并附累计次数（保证"还在持续"这件事不被隐瞒）。
_THROTTLE_STATE = {}
_THROTTLE_LOCK = threading.Lock()
_THROTTLE_DEFAULT_EVERY = 30

def log_line_throttled(key, msg, level="WARNING", every=_THROTTLE_DEFAULT_EVERY):
    """同一 key 的日志：首条必记，其后每 every 条记一次（附累计次数）。
    专供轮询 / 绘制 / 动画这类高频路径使用，避免刷屏挤掉真日志。绝不抛异常。"""
    try:
        with _THROTTLE_LOCK:
            n = _THROTTLE_STATE.get(key, 0) + 1
            _THROTTLE_STATE[key] = n
        if n == 1 or n % max(1, int(every)) == 0:
            log_line("%s（同类已累计 %d 次）" % (msg, n) if n > 1 else msg, level=level)
    except Exception:
        pass

def reset_log_throttle(key=None):
    """复位节流计数（测试用）。key 为 None 时清空全部。"""
    try:
        with _THROTTLE_LOCK:
            if key is None:
                _THROTTLE_STATE.clear()
            else:
                _THROTTLE_STATE.pop(key, None)
    except Exception:
        pass


# v1.4.30：给"兜异常但不处理"的地方用的统一记录口。
# 背景：产品里有 206 处 `except …: pass`（2026-09-24 盘点），这是刻意的韧性设计，
# 但副作用是 **NameError 也会被一起吞掉** —— 真实事故：`_raise_tool_window` 裸调
# 未导入的 `_objc_deps`，工具窗层级提升潜伏几十个版本从未生效（v1.4.29 才发现）。
# 统一走这里，消息里固定带 "SWALLOWED" 前缀，于是一句
#   grep SWALLOWED debug.log
# 就能回答"现在还有没有功能在静默失效"，不必逐个功能去猜。
# 高频路径（16ms 时钟、每帧绘制）安全：内部就是按 key 节流的 log_line_throttled。
def log_swallow(where, exc, level="WARNING", every=_THROTTLE_DEFAULT_EVERY):
    """记录一处"被吞掉的异常"。`where` 写 模块.函数（可带一句后果说明）。

    除了进 debug.log，还会记进同目录的 last_failures.json（限流落盘）——
    用户报"某功能不见了"时，不必先让他点导出诊断包就能定位。
    """
    try:
        _record_swallow(where, exc)
    except Exception:
        # 摘要失败不能拖垮主日志，也不套 log_swallow（会自我递归）；
        # 留下的痕是 internal_faults()["record_swallow"]。
        _note_internal_failure("record_swallow")
    log_line_throttled("swallow:%s" % where,
                       "SWALLOWED %s: %r" % (where, exc), level=level, every=every)


# ---- v1.4.33 "最近静默失效"滚动摘要 --------------------------------------------
# 为什么需要：诊断包只在用户手工点「导出诊断包」时才生成，而"某功能不见了"
# 这类上报通常发生在导出之前；日志又按 5MB×3 代轮转，故障行可能已经被挤出去。
# 一份与轮转无关、只记"哪些站点失效过、各多少次、最后一次什么时候"的小 JSON，
# 让"还有没有功能在静默失效"这个问题在**没拿到日志的情况下**也能回答。
LAST_FAILURES_NAME = "last_failures.json"
LAST_FAILURES_MAX_KEYS = 50
_LAST_FAILURES: dict = {}
_LAST_FAILURES_LOCK = threading.Lock()
_LAST_FAILURES_WRITE_MIN = 5.0        # 已知站点最快 5 秒落盘一次
_last_failures_written_at = 0.0


def last_failures_path():
    """当前生效的摘要路径 —— **调用时**按 LOG_PATH 现算，不在导入时定死。

    为什么不写成模块常量：测试与运行器都在 import 之后重定向 `sf_logging.LOG_PATH`
    （run_tests.sh 把日志送进临时目录），导入期算出的常量会跟着**用户真实的**
    Application Support 目录 —— 那正是本仓反复防着的"测试写脏用户数据"。
    """
    return os.path.join(os.path.dirname(os.path.abspath(LOG_PATH)), LAST_FAILURES_NAME)


def _write_last_failures(snapshot, path=None):
    """原子写摘要：先写 .tmp 再 os.replace，避免读到半截 JSON。绝不抛异常。"""
    if _LOG_MUTED:
        return
    path = path or last_failures_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # 临时名必须**每次唯一**：log_swallow 会从行情线程、绘制线程、更新线程
        # 同时进来（写盘本身在锁外，避免持锁做 IO），共用一个 .tmp 会让两个线程
        # 往同一个文件里交错写，最后 os.replace 提上来的可能是半截 JSON ——
        # 用户真出问题时拿到一个解析不开的摘要，比没有更糟。
        tmp = "%s.%d.%d.tmp" % (path, os.getpid(), threading.get_ident())
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"run": RUN_ID, "version": APP_VERSION,
                       "failures": snapshot}, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    except Exception:
        # 摘要是**辅助诊断物**，写不动就放弃：不能因为它把行情/绘制路径拖住。
        # 痕：internal_faults()["write_last_failures"] 计数 + 文件时间戳不再更新。
        _note_internal_failure("write_last_failures")


def _record_swallow(where, exc):
    """把一处静默失效记进内存并按限流落盘（见上面注释）。"""
    global _last_failures_written_at
    now = time.time()
    with _LAST_FAILURES_LOCK:
        rec = _LAST_FAILURES.get(where)
        stamp = datetime.datetime.now().isoformat(timespec="seconds")
        # 判空写成 `if rec is None` 而不是先存进 is_new：mypy 只对前者的分支
        # 做类型收窄，后者会让下面三行 rec[...] 一直被判成"可能在 None 上下标"。
        if rec is None:
            rec = {"n": 1, "at": stamp, "exc": repr(exc)[:200]}
            _LAST_FAILURES[where] = rec
            is_new = True
        else:
            rec["n"] += 1
            rec["at"] = stamp
            rec["exc"] = repr(exc)[:200]
            is_new = False
        # 站点数封顶：真出风暴时宁可留最新的 N 个，也不能让文件无界增长
        if len(_LAST_FAILURES) > LAST_FAILURES_MAX_KEYS:
            keep = sorted(_LAST_FAILURES.items(), key=lambda kv: kv[1]["at"])
            _LAST_FAILURES.clear()
            _LAST_FAILURES.update(keep[-LAST_FAILURES_MAX_KEYS:])
        snapshot = {k: dict(v) for k, v in _LAST_FAILURES.items()}
        if not is_new and (now - _last_failures_written_at) < _LAST_FAILURES_WRITE_MIN:
            return
        _last_failures_written_at = now
    _write_last_failures(snapshot)


def flush_last_failures_now():
    """无条件把内存摘要写盘（退出收口用）。

    为什么需要：`_record_swallow` 对已知站点最快 5 秒落盘一次，所以文件里的
    次数**可能滞后于现实**。进程正常退出时必须补写一次，否则用户拿到的
    last_failures.json 会把"持续失效 300 次"显示成"失效 1 次"——那就是在
    替故障撒谎。
    """
    with _LAST_FAILURES_LOCK:
        if not _LAST_FAILURES:
            return False
        snapshot = {k: dict(v) for k, v in _LAST_FAILURES.items()}
    _write_last_failures(snapshot)
    return True


def reset_last_failures():
    """清空内存摘要（测试用；不动已落盘的文件）。"""
    global _last_failures_written_at
    with _LAST_FAILURES_LOCK:
        _LAST_FAILURES.clear()
        _last_failures_written_at = 0.0

_REOPEN_CLASS = None

# v1.4.57 —— 这段是 iMac 实机 SIGSEGV 的补丁，别再把它删成"依赖 import 顺序"的形态。
# PyObjC 在**建 Python 子类的那一刻**就要确定每个方法的 ObjC 编码，找不到元数据时
# 一律退化成"返回值和所有参数都是对象(id)"。实测（本机 .venv，pyobjc-core 12.2.2）：
#   只 import Foundation 就建类  → retval '@'  types ['@', ':', '@', '@']
#   先 import AppKit 再建类      → retval 'B'  types ['@', ':', '@', 'B']
# 差别在 hasVisibleWindows 这一位：它是 BOOL。AppKit 派发 reopen Apple Event 时把
# YES(1) 直接放在寄存器里发过来，退化成 '@' 之后 PyObjC 拿 0x1 当对象去调 -self
# （objc_opt_self+8 ← pythonify_c_value ← method_stub ← ffi_closure_SYSV），
# 于是"点 Dock 图标"= EXC_BAD_ACCESS / KERN_INVALID_ADDRESS at 0x1。
# 冷启动时谁先 import 取决于本次走了哪条分支（毛玻璃成没成功、图标画没画过……），
# 同一份包在两台同版本 macOS 上一台正常一台必崩 —— 就是这么来的。
# 这里显式把签名钉死，正确性不再取决于调用顺序。
_REOPEN_SELECTOR = "applicationShouldHandleReopen:hasVisibleWindows:"
_REOPEN_METADATA = {
    "retval": {"type": b"B"},
    "arguments": {2: {"type": b"@"}, 3: {"type": b"B"}},
}


def _pin_reopen_signature():
    """把 reopen 回调的 ObjC 编码钉成 BOOL，返回是否成功。

    失败不静默：签名退回"全是对象"就是那道崩溃的入口，必须留痕（§6 第 9 条）。
    """
    try:
        import objc
        objc.registerMetaDataForSelector(
            b"NSObject", _REOPEN_SELECTOR.encode("ascii"), _REOPEN_METADATA)
        return True
    except Exception as _pin_exc:
        log_swallow("sf_logging._pin_reopen_signature", _pin_exc)
        log_line("reopen signature pin FAILED: %r —— 点 Dock 图标可能崩溃"
                 % (_pin_exc,), "ERROR")
        return False


def _reopen_signature(cls):
    """读出 PyObjC 实际注册给这个 selector 的编码（给闸门和用例用，不参与产品逻辑）。

    返回 (retval, [各参数编码])，取不到时返回 None。
    """
    try:
        sel = _REOPEN_SELECTOR
        sig = getattr(cls, "class")().instanceMethodSignatureForSelector_(sel)
        if sig is None:
            return None
        return (bytes(sig.methodReturnType()),
                [bytes(sig.getArgumentTypeAtIndex_(i))
                 for i in range(sig.numberOfArguments())])
    except Exception as _sig_exc:
        log_swallow("sf_logging._reopen_signature", _sig_exc)
        return None


def _ensure_reopen_class():
    """延迟创建 reopen 转发代理类（首次调用时才 import Foundation，不阻塞冷启动）"""
    global _REOPEN_CLASS
    if _REOPEN_CLASS is not None:
        return _REOPEN_CLASS
    try:
        from Foundation import NSObject
        _pin_reopen_signature()

        class _ReopenForwardDelegate(NSObject):
            _orig = None      # Qt 原 QCocoaApplicationDelegate 实例（强引用防 GC）
            _window = None    # FloatWindow 实例（恢复回调）

            def applicationShouldHandleReopen_hasVisibleWindows_(self, app, flag):
                w = self._window
                if w is not None:
                    try:
                        log_line("reopen called, visible=%r flag=%r" % (w.isVisible(), flag))
                        if not w.isVisible():
                            # 延迟到 Qt 事件循环中恢复，确保窗口真正映射到屏幕
                            QTimer.singleShot(0, w._restore_window)
                    except Exception as _reopen_exc:
                        # 恢复失败用户只看见"点了 Dock 没反应"，必须留痕
                        log_swallow("sf_logging.reopen_delegate.applicationShouldHandleReopen",
                                    _reopen_exc)
                return True

            # 这两个是 ObjC 消息转发的固定搭档：本 delegate 只实现了 reopen 一个 selector，
            # 其余消息须在此补签名并转发给 Qt 原 delegate，否则 Dock 菜单等原生行为整体失效。
            def methodSignatureForSelector_(self, aSelector):
                orig = self._orig
                if orig is not None:
                    try:
                        return orig.methodSignatureForSelector_(aSelector)
                    except Exception:
                        pass
                try:
                    return NSObject.methodSignatureForSelector_(self, aSelector)
                except Exception:
                    return None

            def forwardInvocation_(self, anInvocation):
                orig = self._orig
                if orig is not None:
                    try:
                        anInvocation.invokeWithTarget_(orig)
                        return
                    except Exception:
                        pass
                try:
                    NSObject.forwardInvocation_(self, anInvocation)
                except Exception:
                    pass

        _REOPEN_CLASS = _ReopenForwardDelegate
    except Exception as _cls_exc:
        # 造不出代理类 = 点 Dock 不再恢复窗口，而且没有 reopen 回调可装；
        # 这里必须留痕，否则该功能整条消失也不会在日志里出现一个字。
        log_swallow("sf_logging._ensure_reopen_class", _cls_exc)
        _REOPEN_CLASS = None
    return _REOPEN_CLASS

def _check_recent_crashes():
    """崩溃可观测性（轻量，无第三方 SDK）：启动时扫描用户崩溃报告目录，若有
    本应用 7 天内的崩溃报告则记录到 debug.log，便于发现反复崩溃（11.1）。"""
    try:
        diag = os.path.expanduser("~/Library/Logs/DiagnosticReports")
        if not os.path.isdir(diag):
            return
        hits = []
        # 文件名前缀即崩溃报告的进程名（取 CFBundleName）；改 Info.plist 里的
        # bundle 名时必须同步改这里，否则崩溃永远扫不到、可观测性静默失效。
        for fn in os.listdir(diag):
            if fn.startswith(CRASH_PREFIXES) and fn.endswith((".ips", ".crash")):
                hits.append(fn)
        if not hits:
            return
        newest = max(hits, key=lambda n: os.path.getmtime(os.path.join(diag, n)))
        mt = datetime.datetime.fromtimestamp(os.path.getmtime(os.path.join(diag, newest)))
        if (datetime.datetime.now() - mt).days <= 7:
            log_line("recent crash report: %s @ %s" % (newest, mt), "WARNING")
    except Exception:
        pass


# ---- 2026-10-09 现场锚点：把「现象第一次能被观测到」的时刻装进诊断包 --------------
# 为什么要这一份（台账第八节第一条，也是玻璃层那一类 8 条线上逃逸的共同根子）：
#   「只剩背景 / 内容看不见」的现场只在用户那台机器上。本机日志已经能给出
#   "一次 1278 秒、482 行倒序检出"这种强度，但用户机器上的 debug.log 没有任何回传通路，
#   D-04 / D-31 至今只能写"未确认，需现场取证"。这条不补，这一类缺陷的**定位时长
#   永远记不到**，第二个质量指标在这类缺陷上就是空的。
#   诊断包（右键「导出诊断包」→ 桌面 zip → 发给别人排障）是**已有**的外发通路，
#   所以把锚点并进去，而不是新加一个菜单项，更不是偷偷上传。
# 口径必须写死在数据里：锚点是**程序检出的时刻**，通常早于用户第一次注意到现象；
# 用它当"定位开始"会让时长偏短，别把它当成用户报障时刻。
ANCHOR_WINDOW_MIN = 30
ANCHOR_TAIL_BYTES = 400_000
# 窗口上界的容差：比参照时刻晚 5 分钟以内的行仍然算"最近"。
# 为什么要有这个上界（v1.4.51，台账 G14 的根因之一）：只写下界时，一条**时钟超前**的
# 行（用户改过系统时间、或这份日志来自另一台机器）会永远落在"最近 30 分钟"里，
# 于是"三天前发生过一次"被说成"刚刚还在发生"。留 5 分钟是因为写日志的进程与读日志的
# 参照时刻之间本就可能有毫秒到秒级偏差，而 5 分钟远超实测的正常偏差、又远小于窗口。
ANCHOR_FUTURE_TOLERANCE_SEC = 300
_ANCHOR_LINE_RE = re.compile(
    r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}) ([0-9a-f]{6})\] (\w+): (.*)$")
# needle 全部取自产品里真实在写的日志行（sf_glass._log_retop / _log_blind /
# glass setup / sf_logging.log_swallow），改那些行文字时必须同步改这里 ——
# 锚点扫不到却报"没有锚点"，就是又一枚会说假话的检查。
GLASS_ANCHORS = (
    ("glass_above_main", "re-top: glass above main",
     "背景层压在内容之上（用户视角的「只剩背景、内容看不见」）"),
    ("glass_misaligned", "re-top: glass misaligned", "背景层与内容窗真实错位"),
    ("order_probe_blind", "order probe blind",
     "置顶复核盲区：两窗不在屏幕窗口列表里，看门狗看不见这一对窗"),
    ("geom_resync", "geom resync", "应用侧几何与 WindowServer 不一致（坐标跳变的前置信号）"),
    ("glass_setup_error", "glass setup error", "玻璃层建层失败或降级"),
    ("swallowed", "SWALLOWED", "热路径静默失效留痕"),
)


def recent_log_anchors(minutes: int = ANCHOR_WINDOW_MIN, path: str | None = None,
                        tail_bytes: int = ANCHOR_TAIL_BYTES,
                        now: datetime.datetime | None = None) -> dict[str, Any]:
    """从日志尾部 `tail_bytes` 里，按类别归并最近 `minutes` 分钟的锚点片段。

    只回**事实**，不下结论：每类给首次/末次时刻、行数、涉及哪几次运行(run id)、
    一条原文样本。另外必须如实报两件容易骗人的事：
      · `truncated` —— 只读了尾部，更早的片段看不见，"没有锚点"不等于"没发生过"；
      · `unparsed_lines` —— 旧格式行（`[时:分:秒]`，没有日期与 run id）无法定位到
        绝对时刻，只能整段跳过；把跳过当成没有，就是给排查引偏方向。
    读不到文件/解析失败一律返回带 `error` 的结构，绝不抛出去打断诊断导出。
    `now` 是窗口过滤的参照时刻，默认取当前墙钟；测试注入固定值才能确定性断言时间窗。
    ⚠ 窗口是**双向**的：比参照时刻还晚超过 `ANCHOR_FUTURE_TOLERANCE_SEC` 的行不算"最近"，
    单独计入 `future_lines`。只写下界时，一条时钟超前的行（用户改过系统时间、
    或日志来自另一台机器）会永远落在"最近 30 分钟"里，把"发生过"说成"正在发生"。
    """
    log_path = path or LOG_PATH
    out: dict[str, Any] = {"window_minutes": minutes,
                       "log_basename": os.path.basename(log_path),
           "anchors": [], "runs_seen": [], "unparsed_lines": 0,
           "future_lines": 0,
           "read_bytes": 0, "truncated": False,
           "note": "锚点是程序检出的时刻，通常早于用户第一次注意到现象"}
    try:
        size = os.path.getsize(log_path)
        with open(log_path, "rb") as fh:
            start = max(0, size - tail_bytes)
            fh.seek(start)
            raw = fh.read()
        out["read_bytes"] = len(raw)
        out["truncated"] = start > 0
        text = raw.decode("utf-8", "replace").splitlines()
        if start > 0 and text:
            text = text[1:]          # 半截行不是事实，丢掉而不是猜
        # 参照时刻可由调用方注入：不为测试而存在 —— 用例与真链路都要能证明
        # "三天前那一段不该算进最近 30 分钟"，靠真实墙钟跑这句话就无法确定性地验。
        ref = now if now is not None else datetime.datetime.now()
        cutoff = ref - datetime.timedelta(minutes=minutes)
        buckets: dict[str, dict[str, Any]] = {}
        runs: list[str] = []
        for line in text:
            m = _ANCHOR_LINE_RE.match(line)
            if not m:
                if line.strip():
                    out["unparsed_lines"] += 1
                continue
            stamp = datetime.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S.%f")
            if stamp < cutoff:
                continue
            if stamp > ref + datetime.timedelta(seconds=ANCHOR_FUTURE_TOLERANCE_SEC):
                # 比参照时刻还晚的行不算"最近"（见 docstring 的"窗口是双向的"）。
                out["future_lines"] += 1
                continue
            run, msg = m.group(2), m.group(4)
            if run not in runs:
                runs.append(run)
            for key, needle, desc in GLASS_ANCHORS:
                if needle not in msg:
                    continue
                rec = buckets.get(key)
                if rec is None:
                    buckets[key] = {"key": key, "desc": desc,
                                    "first_seen": stamp.isoformat(timespec="milliseconds"),
                                    "last_seen": stamp.isoformat(timespec="milliseconds"),
                                    "first_run": run, "runs": [run], "hits": 1,
                                    "sample": msg[:160]}
                else:
                    rec["hits"] += 1
                    rec["last_seen"] = stamp.isoformat(timespec="milliseconds")
                    if run not in rec["runs"]:
                        rec["runs"].append(run)
                break
        out["runs_seen"] = runs
        out["anchors"] = [buckets[k] for k, _n, _d in GLASS_ANCHORS if k in buckets]
    except FileNotFoundError:
        out["error"] = "日志文件不存在"
    except Exception as exc:
        out["error"] = "锚点扫描失败：%r" % (exc,)
    return out
