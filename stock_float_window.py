#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A股实时股价悬浮窗（PyQt6：竖向卡片、圆角透明、分时图与按市场状态调度的行情刷新）。"""

import os
import json
import sys
import math
import time
import queue
import threading
import datetime
import traceback
import platform
import tempfile

# ---- v1.4.5 启动位置守卫：必须在 PyQt6 之前（旧副本从 /tmp 误启动会在
# Qt 导入期 SIGSEGV，只留"程序意外退出"弹窗；守卫改为提示后正常退出）----
from sf_bootstrap import guard_temporary_launch
from sf_run_session import RuntimeSessionLedger, session_path_for_log

guard_temporary_launch()

from PyQt6.QtCore import (Qt, QTimer, QPoint, QPointF, QRect, QRectF, pyqtSignal,
                          QCoreApplication, QEvent)
from PyQt6.QtGui import (QAction, QPainter, QColor, QPen, QIcon, QPixmap,
                         QFont, QFontInfo, QFontMetrics, QGuiApplication, QPainterPath,
                         QPolygonF)
from typing import Any, Optional
from PyQt6.QtWidgets import QApplication, QWidget, QToolTip, QProgressBar, QMessageBox
# 退出期同步销毁 Qt 对象用（见 _flush_deferred_deletions / _close_update_flow_box）
from PyQt6.sip import delete as _sip_delete

# 20.1 职责拆分：纯绘制辅助函数下沉到 renderer（超椭圆路径/CGPath/底部四段布局唯一实现）
from renderer import smooth_round_rect_path

# ---- 模块化拆分 sf_ui_render：显式导入（v1.2.22 消除通配符 import*） ----
from sf_ui_render import (BOTTOM_BASE_HIDDEN, BOTTOM_SECTION_HIDDEN, BTN_CY, BTN_X, CHART_MIN_DRAW_H,
                          BTN_ICON_Y, BTN_ICON_SZ, CHART_MIN_H, CHART_TOP, RED, GREEN, NUM_FONT,
                          Y_PRICE, RenderMixin, bottom_rows, collapsed_height,
                          load_bundled_fonts, warm_font_metrics)
# v1.2.95 走查：bottom_rows 此前**从未导入** —— 而 _bottom_texts()（1891 行）直接调用它，
# 运行时抛 NameError，被 _auto_width 的 except 吞掉并记一条 "autowidth error"，于是
# 宽度永不自适应内容，且交易时段按轮询周期刷出 ~20 条/秒错误日志（实测 debug.log 三代
# 轮转里该类错误 12.3 万条，把真故障日志全挤掉）。补上导入即恢复宽度自适应。
# v1.2.43 第2/4项：价格动画局部重绘 —— 脏区计算 + 图表重画判定的纯函数
from sf_ui_render import price_update_rect, should_draw_chart
from sf_ui_render import font_diagnostics, paint_diagnostics   # 诊断：渲染降级末态与字体真值

# ---- 模块化拆分 sf_glass：显式导入 ----
from sf_glass import (GlassMixin, TOOL_WINDOW_LEVEL, active_space_id, install_space_join_filter,
                      native_cocoa_active, content_ns_window)
# v1.4.29：`_objc_deps` 原来只在 sf_glass 里有名分（sf_glass 从 sf_icons 导入它），
# 而本模块的 `_raise_tool_window` 直接裸调它 → 每次调用抛 NameError、被自己下面的
# `except Exception: pass` 吞掉，工具窗抬层从写下那天起一次也没执行过（真机实测：
# 面板/编辑器层级恒为 8，相对顺序恒为"主窗在上"）。教训：**兜异常的 except 里
# 至少要留一条限流日志**，否则一个 NameError 能潜伏几十个版本。
from sf_icons import _objc_deps

# ---- 模块化拆分 sf_editor：显式导入 ----
from sf_editor import EditorWindow

# ---- 模块化拆分 sf_icons：显式导入（含私有名） ----
from sf_icons import show_dock, start_icon_worker, button_pixmaps
from sf_icons import system_icon_mode, connect_appearance_changed   # 图标随系统浅/深色外观切换
from sf_icons import menu_icon   # v1.4.69：右键菜单行首图标（用户看图要求）
from sf_icons import _ICON_QUEUE, _app_ready, _appkit_ready

# ---- 模块化拆分 sf_quote：显式导入（含私有名） ----
from sf_quote import (MinutePoints, fetch_minute, fetch_quote, official_limit_prices,
                      load_api_urls, market_status, price_decimals)
from sf_quote import degraded_source_streak, get_last_quote_source
from sf_quote import get_last_minute_source   # 诊断：分时这一路最近成功走的是哪个源
from sf_quote import ensure_current_quote, quote_is_current
from sf_quote import _is_trading_time
# v1.4.10 功耗：后台线程按需休眠时要卡"交易时段翻转点"，否则会睡过 09:30 开盘
from sf_quote import seconds_to_session_change

# ---- 模块化拆分 sf_scheduler（v1.2.39 上帝类拆分第一步）：轮询间隔决策下沉 ----
from sf_scheduler import compute_poll_interval

# ---- 模块化拆分 sf_animator（v1.2.40 上帝类拆分第二步①）：动画状态机纯逻辑 ----
from sf_animator import anim_duration_ms, crossed_key_level, is_tiny_move, quote_changed

# ---- 模块化拆分 sf_quote_loop（v1.2.40 上帝类拆分第二步②）：行情循环与轮询调度 ----
from sf_quote_loop import (QuoteLoopSchedule, minute_retry_state, next_poll_backoff,
                           ui_poll_interval_ms, ui_top_poll_interval_ms)
# 主窗消息路由：只负责队列消费与行情合批，业务应用仍留在主线程。
from sf_window_message_router import WindowMessageRouter
from sf_update_controller import UpdateController
from sf_update_flow import UpdateFlowController
from sf_diagnostics_controller import DiagnosticsExportController
from sf_window_state import UpdateFlowState
from sf_window_geometry import keep_on_screen_position
from sf_window_lifecycle import WindowLifecycleController
from sf_quote_runtime import QuoteRuntimeController
from sf_quote_presentation import QuotePresentationController
from sf_target_controller import TargetController
from sf_face_guard_controller import FaceGuardController

# ---- 模块化拆分 sf_runtime（v1.2.41 上帝类拆分第三步）：App Nap 省电 / 置顶 / 缩 Dock 运行状态 ----
from sf_runtime import RuntimeState, restore_ui_interval
from sf_performance import PerformanceMonitor
from sf_maintenance import BackgroundMaintenance
from sf_anim_driver import AnimDriver
from sf_stock_transition import StockTransition

# ---- 模块化拆分 sf_network：显式导入（含私有名） ----
from sf_network import QuoteDataError
from sf_network import get_last_http_status
from sf_network import _NET_MONITOR, _NET_POOL

# ---- 模块化拆分 sf_config：显式导入 ----
from sf_config import load_config, save_config
# v1.3.45：首屏数据快照（冷启动预填，消除"占位符 → 真实数据"的内容跳变）
from sf_config import load_last_quote, save_last_quote
from sf_config import load_face_guard_pref, save_face_guard_pref
from sf_config import store_prune_report   # v1.4.60（复核 F7）：剪枝现场进诊断包
from sf_config import _migrate_legacy_config

# ---- 模块化拆分 sf_logging：显式导入（含私有名） ----
from sf_logging import feature_enabled, feature_snapshot, log_line
from sf_logging import log_line_throttled, log_swallow, log_startup_banner
from sf_logging import flush_last_failures_now, last_failures_path
import sf_menu_trace
from sf_logging import _become_single_instance, _check_recent_crashes, _ensure_reopen_class

# v1.4.40 人脸在场守卫：顶层导入是安全的 —— sf_presence 本身只依赖标准库与
# sf_logging，Vision/AVFoundation 的装载推迟到它内部的 _load_frameworks()
# （只有用户在菜单里真的打开这个功能时才执行），所以没开功能的用户不会在
# 启动路径上多付一次框架装载。
import sf_presence
import sf_dock_motion

# ---- 模块化拆分 sf_utils：显式导入（含私有名） ----
from sf_utils import fmt_pct
from sf_utils import _drop_flash_due

# ---- 模块化拆分 sf_constants：显式导入（含私有名） ----
from sf_constants import (ANIM_LARGE_MS, ANIM_LARGE_PCT, ANIM_NORMAL_MS, ANIM_TINY_PRICE,
                          APP_DISPLAY_NAME, APP_VERSION, CONFIG_BACKUP_PATH, CONFIG_PATH,
                          # v1.4.41 复核 F2：守卫"采不到画面"的判中断时长与自愈退避。
                          # 从 sf_constants 直取（唯一来源），不绕 sf_presence 转发。
                          FACE_GUARD_FIRST_FRAME_SEC, FACE_GUARD_MENU_TEXT,
                          FACE_GUARD_RETRY_STALLED_SEC, FACE_GUARD_SCREEN_PROBE_SEC,
                          FACE_GUARD_STALE_SEC,
                          FG_BRIGHT_HEX, FG_DIM, FG_DIM_HEX, FG_HINT_HEX, ACCENT_BLUE_HEX, MENU_SEPARATOR_HEX,
                          UP_RED_HEX, DOWN_GREEN_HEX,
                          LAST_QUOTE_PATH, LOG_PATH, MAX_H, MIN_H, W, LOG_ROTATE_KEEP, POLL_DEGRADED,
                          LOG_MAX_BYTES,
                          POLL_FAIL_TO_DEGRADED, POLL_FAIL_TO_OPEN,
                          POLL_INTERVAL_DEGRADED, POLL_INTERVAL_MINIMIZED,
                          POLL_INTERVAL_NORMAL,
                          POLL_INTERVAL_OFF_MARKET, POLL_NORMAL,
                          RELEASE_STAGE, BUNDLE_ID, LEGACY_BUNDLE_IDS,
                          UI_WATCHDOG_INTERVAL_MS,
                          BETA_SUFFIX, MENU_VERSION_FONT_PX, MENU_VERSION_LETTER_SPACING_PX,
                          LAST_QUOTE_MIN_WRITE_INTERVAL, UPDATE_INITIAL_DELAY_MS,
                          TEMP_SWEEP_DELAY_MS, STAGING_PRUNE_DELAY_MS,
                          UPDATE_PERIODIC_MS, UPDATE_RESUME_MIN_GAP_SEC,
                          UPDATE_RETRY_FIRST_MS, UPDATE_RETRY_LATER_MS,
                          BELL_BUBBLE_DWELL_MS, BELL_BUBBLE_HIDE_GRACE_MS)

# ---- 再导出契约（v1.3.51 显式登记，禁止当死代码删掉）----
# 下面这批符号在本文件正文里**零使用**，看起来像导入冗余，实际是刻意保留的再导出：
#   RED、GREEN（上文 sf_ui_render 导入行）、FG_DIM、POLL_DEGRADED、
#   POLL_INTERVAL_DEGRADED、POLL_INTERVAL_NORMAL（以上三个在紧邻的 sf_constants 导入里）、
#   _migrate_legacy_config（sf_config 导入行）、_drop_flash_due（sf_utils 导入行）、
#   fmt_pct（sf_utils 导入行）。
# 保留理由有两条，任一条都足以让删除立刻出事：
#   1. 测试以 m.符号 的形式直接访问（test_render.py / test_core.py / test_migrate.py /
#      test_v165.py / test_v123.py），删了就是一片 AttributeError；
#   2. 宿主模块已下沉为真实实现，这些常量在运行时被真实调用——证据：
#      _migrate_legacy_config → sf_config.load_config 在用；
#      _drop_flash_due       → sf_anim_driver 在用；
#      POLL_INTERVAL_NORMAL  → sf_scheduler 在用；
#      FG_DIM                → sf_ui_render 渲染中性灰在用。
# v1.3.46 瘦身时靠人工记住才没删，v1.3.51 起把这条契约写进代码。以后再瘦身，
# 请先确认测试与宿主模块都不再引用，再动这几行。

# ---- 模块化拆分「目标价」功能（v6 Step 4/6/7） ----
from targets import (evaluate_tick, recover_alerts, expired, drop_expired,
                     bell_bubble_rows,
                     _beijing_now, _clear_dt, STATE_HIT,
                     roll_alerts_to_trading_day)
from sf_notify import (has_bundle_identity,
                      delivery_stats as _notify_delivery_stats,
                       deliver as _notify_deliver, request_authorization,
                       semantic_key, target_notification_title)
from sf_calendar import calendar_sync_status, is_trading_day_confirmed, prefetch_months_async
from sf_config import load_alerts, save_alerts
from sf_target_lifecycle import TargetLifecycleCoordinator
from sf_quote_delivery import TargetTickBuffer
from sf_storage_tasks import SnapshotWriter
from sf_macos_menu import install_window_actions

# 崩溃报告关键段抽取（纯函数，供单测，无 Qt 依赖）
from sf_crashreport import (extract_crash_key_sections, is_crash_report_name,
                            select_crash_reports)

# 网络接口与敏感配置收敛（便于统一维护/替换）
# 东财 suggest 半公开接口 token：默认值仅用于关键词联想搜索（非敏感资金类）。
# v1.2.28：token 设置功能（config "eastmoney_token" / Keychain / 环境变量覆盖）已移除，
# 固定使用内置默认 token（sf_constants.EASTMONEY_TOKEN_DEFAULT），保证开箱即用。

# ---- 8.1 v1.2.5 Feature Flag（方案第八章）----
# 每项新功能独立开关。开启来源优先级：环境变量 STOCKFLOAT_FEATURES（逗号分隔）
# > 同目录 features.ini > sf_constants._FEATURE_DEFAULTS 中的各项默认值。

# v1.2.6：主视图底部居中显示的版本号样式（10pt 加粗浅灰）

# 5.2 v1.2.5：价格滚动动画参数（V2.0 细化版，常规 120~150ms，大幅 200ms，微小直接赋值）

# 5.5 v1.2.5：轮询熔断状态机（交易时段生效；非交易时段间隔见 sf_constants）

# 6.4 v1.2.5：网络连接复用（连接池）参数

# 5.1 v1.2.5：网络监控埋点参数

# 日志轮转：debug.log 超过该大小（字节）后滚动为 debug.log.1，防止长期占满磁盘
# 11.1 日志按时间清理：轮转旧档（debug.log.1 / debug.log.2 ...）超过该天数即删除，
# 避免旧档无限累积长期占磁盘；当前日志本身由滚动机制控制在 5MB 内。

# 配置存储规范化：v1.0.22 起存 macOS 规范目录，旧版根目录配置自动迁移

# 全局异常兜底：未捕获异常（含后台线程/槽函数）记录日志，避免静默失败
def _bundle_version():
    """读取打包后 .app 的 Info.plist 版本号（CFBundleShortVersionString 优先，缺失回退
    CFBundleVersion）；开发态运行（无 Info.plist）回退 sf_constants.APP_VERSION。"""
    try:
        import plistlib
        plist = os.path.abspath(os.path.join(os.path.dirname(sys.executable), '..', 'Info.plist'))
        if os.path.exists(plist):
            with open(plist, 'rb') as f:
                _d = plistlib.load(f)
            ver = _d.get('CFBundleShortVersionString') or _d.get('CFBundleVersion')
            if ver:
                return str(ver)
    except Exception:
        pass
    return APP_VERSION


def _running_bundle_id():
    """此刻**正在运行**的那个 .app 的 CFBundleIdentifier；开发态返回 ""。

    为什么诊断包要它（改名/OTA 分叉那一族）：sf_constants 里的 BUNDLE_ID 只能说明
    "这份源码认为自己是谁"，说明不了用户机器上被拉起的到底是哪一代 —— 旧副本从
    /tmp 被 LaunchServices 唤起（v1.4.5 那枚 SIGSEGV 的成因）、或装着 ≤1.4.54 的旧包
    却读到新清单，都是"两个身份在同一台机器上并存"。这一项把实际那个读出来。
    """
    try:
        import plistlib
        plist = os.path.abspath(os.path.join(os.path.dirname(sys.executable), '..', 'Info.plist'))
        if os.path.exists(plist):
            with open(plist, 'rb') as f:
                _d = plistlib.load(f)
            if _d.get('CFBundleIdentifier'):
                return str(_d['CFBundleIdentifier'])
    except Exception as exc:
        log_line("diagnostics bundle id unreadable: %r" % (exc,), "WARNING")
    return ""


def _menu_icon_evidence():
    """菜单行首图标的**缓存现状**（不调 menu_images()：取诊断不许顺手把图标全建一遍）。

    三种长相必须分得开，它们对应三种不同的用户说法：
      · `built=false` —— 这一轮还没人打开过菜单（什么都还没发生，不是故障）；
      · `missing=[...]` —— 出过图但某几行取不到（QtSvg 缺失/渲染失败），界面上那一行
        **不放图标**、退回"无图标 + 勾号"；
      · `missing=[] 且 built=true` —— 菜单行图标都出图了，看不见就另有原因，这条至少把范围
        从"出图"挪到"合成"。
    """
    import sf_constants
    import sf_icons
    cache = getattr(sf_icons, "_menu_images_cache", None)
    keys = list(getattr(sf_icons, "MENU_ICON_KEYS", ()))
    return {
        "built": cache is not None,
        "registered": len(keys),
        "missing": sorted(k for k in (cache or {}) if (cache or {}).get(k) is None),
        "icon_pt": sf_constants.MENU_ICON_PT,
        "device_ratio": getattr(sf_icons, "MENU_ICON_DEVICE_RATIO", None),
    }


def _peek_install_result():
    """上一轮自动更新助手留下的结果（installed / rolled_back / …），**不删文件**。

    单独一层包装是为了让诊断导出绝不碰 `consume_install_result()`：那条在 finally 里
    unlink，导出一次就把用户下次启动本该看到的"装好了/装失败"提示抢走了。
    """
    import sf_update
    return sf_update.peek_install_result()


def _update_inventory():
    """更新通道的落盘现场：/Applications 里的隐藏回退包 + staging 残留（S38）。

    末态里再带一份"本进程刚清理过什么"（`prune`）—— 只写日志会被轮转挤掉，
    而"我上一次更新失败留下的半包还在不在"恰恰是需要远程回答的问题。
    """
    import sf_update
    out = sf_update.update_storage_inventory()
    out["prune"] = sf_update.staging_prune_report()
    return out


def _publish_diagnostics_file(temporary, requested):
    """Publish a completed ZIP without overwriting another export or a symlink."""
    stem, suffix = os.path.splitext(requested)
    for index in range(1, 1001):
        candidate = requested if index == 1 else "%s-%d%s" % (stem, index, suffix)
        try:
            os.link(temporary, candidate)
        except FileExistsError:
            continue
        return candidate
    raise OSError("too many diagnostic archives with the same name")


def _set_menu_icon(act, key, tag=None):
    """给右键菜单的一行贴上行首图标（v1.4.69，用户看图要求）。

    放在模块级而不是 contextMenuEvent 里，是因为菜单开着时**两态图标要跟着状态换**
    （`_refresh_face_guard_menu_check` 每 300ms 刷一次），两处必须走同一个动作 ——
    写成弹出路径里的局部函数，刷新那一处就够不着，只会画出一张停在弹出时刻的旧图。

    `menu_icon` 取不到图（无 QtSvg / 渲染失败）时返回 None，这里什么都不做：
    那一行退回"无图标 + 勾选框"的老形状，开关状态照样看得见（降级不丢信息）。
    """
    _ic = menu_icon(key)
    if _ic is not None and act is not None:
        act.setIcon(_ic)
    if tag is not None and act is not None:
        act.setData(tag)    # 固定名字：菜单会话记录按它认是哪一项，不依赖文案与行号


def _menu_version_font():
    """右键菜单版本行的字体：股价同款数字字体 D-DIN，常规字重，像素字号与绝对字距取自 sf_constants。

    返回 None 表示 D-DIN 没解析到（字体文件缺失或没注册）——调用方此时不给这一行单独设字体，
    保持菜单原有字体。不带着一个落到兜底字体、又被加了字距的字形上屏：那样会比现状更松散。
    字号用像素而不是磅，与样式表里 `font-size: 13px` 同一单位；字距用 AbsoluteSpacing，
    单位是像素，不随字号缩放（QFont.PercentageSpacing 是另一个量纲，别混）。
    """
    f = QFont(NUM_FONT)
    if QFontInfo(f).family() != NUM_FONT:
        return None
    f.setPixelSize(MENU_VERSION_FONT_PX)
    f.setWeight(QFont.Weight.Normal)
    f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, MENU_VERSION_LETTER_SPACING_PX)
    return f


def _menu_context(window):
    """右键菜单会话里只有主窗才知道的几项现场；每项独立取，取不到就是 None，不外抛。

    in_fullscreen_space 与 glass.json 同一个口径：当前活动 Space 不是记过的普通 Space；
    还没记过普通 Space 的基线时是 None（不可信，不猜）。"""
    out: dict[str, Any] = {"main_visible": None, "main_active": None, "in_fullscreen_space": None}
    try:
        out["main_visible"] = bool(window.isVisible())
        out["main_active"] = bool(window.isActiveWindow())
        if native_cocoa_active():
            space = active_space_id()
            normal = getattr(window, "_normal_space_id", None)
            if space is not None and normal is not None:
                out["in_fullscreen_space"] = space != normal
    except Exception as exc:
        log_swallow("stock_float_window._menu_context", exc)
    return out


def _install_global_excepthook():
    def _hook(etype, val, tb):
        try:
            # v1.2.23 增强：崩溃信息自动收集 —— 时间戳 + 关键上下文（版本/系统/
            # 线程/当前股票代码）+ 完整异常堆栈写入 debug.log（ERROR 级经统一日志
            # 通道同时进 macOS 统一日志，便于 `log show` 检索）。
            # 安全：仅记录非敏感上下文，不写入任何凭据、token 或配置全文。
            stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cur = threading.current_thread()
            tname = cur.name if cur is not threading.main_thread() else "main"
            ctx = "v%s / %s / thread=%s" % (
                APP_VERSION,
                platform.platform() if hasattr(platform, "platform") else "macOS?",
                tname,
            )
            win = globals().get("_win_ref")
            code = getattr(win, "code", None) or getattr(win, "_code", None)
            if code:
                ctx += " / code=%s" % code
            name = getattr(etype, "__name__", str(etype))
            body = "".join(traceback.format_tb(tb))
            try:
                _log_dir = os.path.dirname(LOG_PATH)
                if _log_dir:
                    os.makedirs(_log_dir, exist_ok=True)
                with open(LOG_PATH, "a", encoding="utf-8", errors="replace") as f:
                    f.write("UNCAUGHT %s [%s] %s: %s\n%s\n" % (stamp, ctx, name, val, body))
                os.chmod(LOG_PATH, 0o600)
            except Exception as exc:
                # 文件落盘失败不应阻止独立的统一日志记录。
                log_line("UNCAUGHT file log failed: %r" % (exc,), "ERROR")
            log_line("UNCAUGHT %s [%s] %s: %s" % (stamp, ctx, name, val), "ERROR")
        except Exception as exc:
            try:
                log_line("UNCAUGHT hook failed: %r" % (exc,), "ERROR")
            except Exception:
                pass
    sys.excepthook = _hook
    try:
        # v1.2.71 第4轮走查修复：threading.excepthook 的调用签名与 sys.excepthook
        # **不同** —— 它被调用时只传入一个 threading.ExceptHookArgs 对象，
        # 而 _hook 需要 (etype, value, tb) 三个位置参数。此前直接
        # `threading.excepthook = _hook` 会让钩子自身抛 TypeError
        # （控制台只留一行 "Exception in threading.excepthook"），
        # 原始异常类型与堆栈全部丢失 —— 图标工作线程、编辑器线程等任何子线程
        # 的崩溃既不进 debug.log 也不进统一日志，崩溃上报对线程崩溃完全空转。
        # 这里做参数适配，把 ExceptHookArgs 拆成三参交给同一套记录逻辑。
        threading.excepthook = lambda args: _hook(
            args.exc_type, args.exc_value, args.exc_traceback)
    except Exception:
        pass


def _ensure_update_controller(owner):
    """给真实窗口和旧式 fake host 提供同一个更新后台边界。"""
    controller = getattr(owner, "_update_controller", None)
    if controller is not None:
        return controller
    enqueue = getattr(owner, "_q_put", lambda _message: None)
    controller = UpdateController(enqueue, _bundle_version)
    owner._update_controller = controller
    return controller


_UPDATE_FLOW = UpdateFlowController(
    _ensure_update_controller, lambda *args: log_line(*args),
    lambda *args: log_swallow(*args))


def _normalize_code_6(code):
    """v1.2.48(B2)：切股丢弃判断用代码归一 —— 去市场前缀，纯数字统一补 0 到 6 位
    （北交所 4 位老代码 8307→830700）。非纯数字/无法归一返回 None（不参与比较）。
    与 sf_utils.normalize_code 的区别：后者对带前缀代码原样返回不做 6 位归一，
    而行情回包与自选代码可能一侧带前缀、一侧补 0，直接比较会误判切股。"""
    c = (code or "").strip().lower()
    for p in ("sh", "sz", "bj"):
        if c.startswith(p):
            c = c[len(p):]
            break
    if not c.isdigit():
        return None
    if len(c) < 6:
        c = c.ljust(6, "0")
    return c


def _lazy_attr(owner, name, factory):
    """只在第一次访问时创建代理对象，避免热路径反复构造默认值。"""
    value = owner.__dict__.get(name)
    if value is None:
        value = factory()
        owner.__dict__[name] = value
    return value

# ---- 8.1 reopen 转发代理 ----
# 点击 Dock 图标恢复悬浮窗依赖 NSApplicationDelegate 的
# applicationShouldHandleReopen:hasVisibleWindows: 回调。PyQt6 的
# QCocoaApplicationDelegate 由 Qt 内部创建并设为 NSApp.delegate；历史实现用
# class_replaceMethod 直接替换其类方法（依赖 Qt 私有类名，Qt 升级会静默失效）。
# 现改为自定义 delegate 转发代理：只接管 reopen，其余所有 delegate 消息经
# methodSignatureForSelector_/forwardInvocation_ 转发给 Qt 原 delegate，
# 不触碰 Qt 私有类，Qt 版本升级也不受影响。

# v1.2.65 第1轮走查：原 COLLAPSED_GAP(=14)、COLLAPSED_ROW_H(=7) 已删除。
# 二者描述的是 v1.2.62 之前的旧布局（涨跌幅独占 Y_PCT 行、底部仅一行高/低），
# 改双行两段后即无人引用，但定义与注释一直残留 —— 注释仍在解释 Y_PCT+H_PCT 的
# 旧公式（Y_PCT/H_PCT 早已随涨跌幅上移而废弃），与代码严重漂移，易误导后续维护。
# 收起态高度现由 sf_ui_render.collapsed_height() 唯一实现（含 PRICE_TO_BOTTOM_GAP），
# 行内字形高统一用 BOTTOM_ROW_ASCENT，不再有第二份定义。
# 5.4 v1.2.5：展开/收起分时图（V2.0 细化版：改窗口总高度而非图区高度）
CHART_H = 72                      # 展开态分时图区高度（方案细化值）
LAYOUT_ANIM_DUR = 0.20            # 收展布局动画时长（秒，200ms smoothstep）
# v1.4.28 跟手性：手动拉伸期间 16ms 合并时钟**不停表**的保持窗口（秒）。
# 每收到一个拉伸鼠标事件就续期一次；正常松开会立刻清掉 _resizing，这个上限只
# 用来兜住"macOS 偶发不投递 mouseRelease"（见 mousePressEvent 里的粘滞手势注释），
# 避免定时器像 v1.2.65 修过的那样永久空转。
RESIZE_HOLD_SEC = 2.0
CHART_EXPANDED_MINUTE_INTERVAL = 1.0   # 展开态分时拉取间隔（秒，交易时段）
CHART_COLLAPSED_MINUTE_INTERVAL = 30.0 # 收起态分时拉取间隔（秒，仅兜底刷新）
# v1.2.30：分钟分时拉取失败时短期快速重试 —— 缩小启动期/空返回期的无数据窗口。
# 失败后以 MINUTE_RETRY_INTERVAL（2.5s）连续重试，拿到有效数据即恢复正常周期；
# 连续 MINUTE_RETRY_MAX 次仍失败则回落常规周期（展开1s/收起30s/非交易60min），
# 避免长时间高频空转刷屏；与交易时段判定/30s 崩溃重启保护互不影响。
MINUTE_RETRY_INTERVAL = 2.5
MINUTE_RETRY_MAX = 5
BG = QColor("#1b1e2b")
BORDER = QColor("#3b4256")
GRAY = QColor("#9aa2b8")


# 顶部按钮布局（右上角四按钮，中心 x = width - 偏移，间距 22px）
# v1.3.58：原 HIT_DX=10 在相邻中心距 22px 下留下 2px 点击空档；触控板点在
# 图标边缘会完全不命中，表现为目标价/缩小偶发无响应。扩至 11 后热区刚好相接，
# _hit_btn 已按最近中心消解边界归属，不会串到相邻按钮。
HIT_DX = 11
HIT_DY = 16        # 点击热区纵向半高：32px，覆盖正常触控板轻微漂移
PRESS_RELEASE_SLOP = 6  # 同一按下动作允许 6px 释放漂移；小于相邻中心距的一半，不会跨按钮

# 布局常量（横向边距与内容区宽度）
# 布局常量（纵向位置与高度）
# 顶部留白：名称/代码等左侧内容整体随 TOP_GAP 定位；按钮区保持不动。
# TOP_GAP 定义见顶部「窗口尺寸常量」区
# 字号
# v1.2.64：原 FONT_CODE_ASCENT = 10 已删除 —— 全项目零引用（死常量），
# 且 v1.2.64 新增的 sf_ui_render.BOTTOM_ROW_ASCENT 是同值的第二份定义，
# 留着会形成「双份漂移」（改一处忘另一处）。底部区 ascent 统一用 BOTTOM_ROW_ASCENT。
# v1.2.65 第1轮走查：COLLAPSED_ROW_H = 7 已删除 —— 高度公式改走
# sf_ui_render.collapsed_height() 后全项目零引用。它与 BOTTOM_ROW_ASCENT(=10)
# 描述的是同一个物理量（9pt 文字顶→baseline），数值却不同：7 是**纯数字**的
# 视觉高度（cap height），10 是含汉字行的真实 ascent。保留会让后来者误用。
# 顶部按钮图标（SF Symbols，10px）

# 顶部按钮图标（用户上传 SVG，内嵌资源，保持 10px 显示尺寸；颜色沿用 SVG 内 #EFEFEF）
# 底部高/低行：基线距窗口底 BOTTOM_BASE_HIDDEN(17)，高/低行贴窗口底约 3.5px 留白

# 背景模式：solid=纯黑 / translucent=半透明黑 / glass=系统毛玻璃（NSVisualEffectView）

# 边缘吸附：拖拽松手后距屏幕边缘小于该距离（pt）时自动贴边
# ⚠ 来源（v1.4.59 按"阈值必须标来源"这条规矩补的诚实说明）：**没有实测依据**。
#   32pt 是 v1.2.x 一路沿用下来的手感值，本仓不采集用户落点分布，因此今天也给不出
#   分布图。要改这个数，先补一个可复现的测量（例如用 tools/ 的跟手性探针统计松手点
#   到屏幕边缘的距离分布），别凭手感再拍一个。
SNAP_MARGIN = 32

# 网络异常：行情连续失败达到该次数时，名称栏显示"网络异常"
NET_FAIL_SHOW = 3

# 备用行情源（4.1 降级）：主源（腾讯）异常/网络失败时自动切换，避免单源故障长期"网络异常"

# 1.3 v1.2.0：API 地址收敛到 config.json（"api_urls" 字段），改地址无需重新打包。
# API_URLS_DEFAULT 为内置默认；load_api_urls() 读取 config 中同名 key 覆盖对应全局常量。

# 5.0 v1.2.0：config.json 结构版本号。load_config 兼容无 version 的旧配置（视为 1）；
# save_config 写入 version 并保留已有字段。后续结构变更时递增。

# 1.1 v1.2.0：股价右上角状态徽标（涨停/跌停/停牌）宽度，价格大字区右边界让出该宽度避免重叠

# 6.0 v1.2.0：诊断增强——模块级记录最近一次 HTTP 状态码与成功数据源，
# 导出 diagnostics.txt 时写入 meta，便于反馈问题快速定位。


# 日志按时间清理（11.1）：轮转时删除超过该天数(天)的滚动旧档 debug.log.N

# 接口格式错误升级（4.2）：连续 FORMAT_ERROR_ALERT_THRESHOLD 次格式错误（非网络错误）
# 输出醒目 ERROR 诊断日志，便于感知非官方接口格式变更；正常流程不受影响。

# 跌破整数关口时价格闪烁的高亮色（黑色背景上最醒目）
FLASH_DUR = 0.6    # 闪烁持续时长（秒）
FLASH_IVAL = 120   # 闪烁交替间隔（毫秒）

# 12.x v1.2.11 股价下跌闪烁提醒：跌幅达档位阈值时股价文字闪烁警示
FLASH_DROP_PCT_1 = 4.0      # 第一档：跌幅 ≥4% 触发一次
FLASH_DROP_PCT_2 = 7.5      # 第二档：跌幅 ≥7.5% 再触发一次
FLASH_DROP_COOLDOWN = 180.0  # v1.2.58：每档触发后 3 分钟冷却，只触发一次不再反复打扰
FLASH_DROP_IVAL_MS = 350    # 闪烁交替间隔（毫秒），规格 0.3~0.5s 取 0.35s
FLASH_DROP_PHASES = 6       # 交替相位总数（亮-灭-亮-灭-亮-灭 = 3 个来回）
FLASH_DROP_LEVELS = (       # 档位表：key 用于独立冷却记录
    {"key": "pct4",  "pct": FLASH_DROP_PCT_1},
    {"key": "pct75", "pct": FLASH_DROP_PCT_2},
)

# 12.1 token 安全：Keychain 服务/账户名（ctypes 调 Security.framework，不新增 pyobjc 依赖）

# ---- 4.2 接口格式错误升级告警（非网络错误）----

# ---------- 冷启动优化：图标后台生成 ----------
# AppKit 首次 import 是冷启动最大开销之一，放后台线程与 QApplication 构造并行；
# 图标一律生成 QImage（线程安全）后经队列回传 GUI 线程转 QPixmap/QIcon 应用。


class TargetBubble(QWidget):
    """铃铛专用自绘气泡（效果图 B 版）：待触发 / 已触发两组，每行「方向词｜价位｜距现价%」。

    窗口比可见的气泡本体大一圈透明边（MARGIN_*），用来画柔影和指向铃铛的小三角。
    所有尺寸与定位接口（show_below / body_rect_global）都以「本体」为准，调用方不必关心这圈边。
    """
    # ---- 本体（单位 px，取自效果图 B 的实测值）----
    PAD_X = 12
    PAD_TOP = 9
    PAD_BOTTOM = 10
    RADIUS = 10
    MIN_CONTENT_W = 112         # 内容区最小宽（不含左右内边距）
    HEADER_H = 15
    HEADER_GAP = 3
    ROW_H = 22
    SEP_GAP_TOP = 7             # 两组之间：细线上方 7、下方 6
    SEP_GAP_BOTTOM = 6
    COL_GAP = 6
    PCT_GAP = 14                # 价位与百分比之间，在列距之外再留的空
    # ---- 底边对齐（2026-10-23 效果图 B 方案）----
    # 三列共用一条基线，但「涨到/跌到」的笔画底部实测比数字低约 1px（8 倍屏量：方向词 1.125、价位与百分比
    # 都是 0.125，差 1.0），所以方向词整体上提 WORD_RAISE，让可见的底边落在同一条线上。
    # 组标题「待触发/已触发」的文字底边比基线低 HEADER_INK_OVER（实测 1.0），前面的圆点底边与它齐平。
    # 两个数是按这两套字体量出来的设计取值；换字体或字号要重量，不要凭感觉改。
    WORD_RAISE = 1.0
    HEADER_INK_OVER = 1.0
    DOT_SIZE = 6
    # ---- 与铃铛的关系 ----
    GAP_TO_ICON = 8             # 本体边到铃铛图标边；悬停底板比图标各大 4px，三角尖端正好顶到底板
    CARET_H = 4.25
    CARET_INSET = 15            # 三角中心离本体左右边至少这么远，避开 10px 圆角
    SCREEN_EDGE = 4             # 本体离屏幕可用区域边缘至少留的空
    # ---- 透明外圈：柔影与三角都画在这里 ----
    MARGIN_X = 16
    MARGIN_TOP = 12
    MARGIN_BOTTOM = 24
    # ---- 颜色（涨跌色取 sf_constants 的具名 token，与主界面 RED / GREEN 同一来源，同屏只此一种红）----
    UP_COLOR = QColor(UP_RED_HEX)
    DOWN_COLOR = QColor(DOWN_GREEN_HEX)
    PENDING_DOT_COLOR = QColor("#f5c542")     # 与铃铛「待触发」同色
    HIT_DOT_COLOR = QColor("#9aa2b8")         # 与铃铛「已触发」同色
    HEADER_COLOR = QColor(FG_DIM_HEX)
    HIT_TEXT_COLOR = QColor(FG_HINT_HEX)
    BODY_COLOR = QColor(29, 30, 28, 245)
    RING_COLOR = QColor(255, 255, 255, 26)
    SEP_COLOR = QColor(255, 255, 255, 20)

    def __init__(self):
        super().__init__(None)
        # WindowTransparentForInput 才是让「原生窗口」点击穿透的开关（macOS 上对应 NSWindow 的
        # ignoresMouseEvents）。下面那条 WA_TransparentForMouseEvents 只管 Qt 内部的事件传递，
        # 2026-10-23 实测不加这个标志时 ignoresMouseEvents=False：气泡窗口带透明外圈，顶边落在铃铛
        # 感应区下半部分，鼠标停在那里会「进入」气泡窗口 -> 主窗收到离开 -> 气泡收起 -> 鼠标回到
        # 主窗 -> 再弹出，来回闪。两个都留着：前者管原生层，后者管 Qt 层。
        self.setWindowFlags(Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.WindowTransparentForInput)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)  # 提示气泡不得截获顶部按钮点击
        self._rows = {"pending": [], "hit": []}
        self._body_w = self._body_h = 0
        self._flipped = False
        self._caret_x = 0.0          # 三角中心，本体坐标系（不含透明外圈）
        self._relayout()

    @staticmethod
    def _fonts():
        """每次现建：字体可能在气泡之后才注册（load_bundled_fonts），缓存会固化兜底字体的度量。"""
        price = QFont(NUM_FONT)
        price.setPixelSize(15)
        price.setBold(True)
        pct = QFont(NUM_FONT)
        pct.setPixelSize(11)
        pct.setBold(True)
        word = QFont("PingFang SC")
        word.setPixelSize(11)
        header = QFont("PingFang SC")
        header.setPixelSize(10)
        header.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.4)
        return price, pct, word, header

    def _columns(self):
        """三列各自的宽度；没有任何百分比时第三列为 0，整块自动收成两列。"""
        price_f, pct_f, word_f, _header_f = self._fonts()
        rows = self._rows["pending"] + self._rows["hit"]
        word_w = max((QFontMetrics(word_f).horizontalAdvance("涨到"),
                      QFontMetrics(word_f).horizontalAdvance("跌到")))
        price_w = max((QFontMetrics(price_f).horizontalAdvance(r[1]) for r in rows), default=0)
        pct_w = max((self._stable_advance(pct_f, r[2]) for r in rows), default=0)
        return word_w, price_w, pct_w

    @staticmethod
    def _stable_advance(font, text):
        """文字宽度：每个数字都按「最宽的那个数字」量。

        内置数字字体的数字不等宽（实测 11 号粗体：「1」宽 4px，「0/3/4/6/8/9」宽 6px），
        百分比随行情每跳一次，直接量字符串会让整块气泡在 1~2px 之间来回变宽变窄，
        居中对齐使左边缘跟着横向抖。按最宽数字量之后，只要位数、符号、小数点相同，
        宽度就恒定；画的时候仍用真实字形右对齐，所以看到的字形不变。"""
        fm = QFontMetrics(font)
        widest = max("0123456789", key=fm.horizontalAdvance)
        return sum(fm.horizontalAdvance(widest if c.isdigit() else c) for c in text)

    def _relayout(self):
        _price_f, _pct_f, _word_f, header_f = self._fonts()
        word_w, price_w, pct_w = self._columns()
        content_w = word_w + self.COL_GAP + price_w
        if pct_w:
            content_w += self.COL_GAP + self.PCT_GAP + pct_w
        hfm = QFontMetrics(header_f)
        header_w = max((hfm.horizontalAdvance(t) + 12
                        for t, k in (("待触发", "pending"), ("已触发", "hit")) if self._rows[k]),
                       default=0)
        self._body_w = int(math.ceil(max(content_w, header_w, self.MIN_CONTENT_W))) + 2 * self.PAD_X
        h = self.PAD_TOP + self.PAD_BOTTOM
        groups = [k for k in ("pending", "hit") if self._rows[k]]
        for k in groups:
            h += self.HEADER_H + self.HEADER_GAP + self.ROW_H * len(self._rows[k])
        if len(groups) == 2:
            h += self.SEP_GAP_TOP + 1 + self.SEP_GAP_BOTTOM
        self._body_h = max(h, 40)
        self.setFixedSize(self._body_w + 2 * self.MARGIN_X,
                          self._body_h + self.MARGIN_TOP + self.MARGIN_BOTTOM)
        self.update()

    def setRows(self, pending, hit):
        """设置两组行数据，每行 (is_up, 价位文本, 百分比文本)。内容没变就什么都不做，返回是否变化。"""
        new = {"pending": [tuple(r) for r in (pending or [])],
               "hit": [tuple(r) for r in (hit or [])]}
        if new == self._rows:
            return False
        self._rows = new
        self._relayout()
        return True

    def rows(self):
        return {k: list(v) for k, v in self._rows.items()}

    @staticmethod
    def _available_rect(anchor_global):
        """锚点所在屏幕的可用区域（已去掉菜单栏与 Dock）。取不到屏幕就返回一个极大的矩形，等于不收拢。"""
        try:
            scr = QGuiApplication.screenAt(anchor_global) or QGuiApplication.primaryScreen()
            if scr is not None:
                return scr.availableGeometry()
        except Exception as _swallow_exc:
            log_swallow("stock_float_window.TargetBubble._available_rect", _swallow_exc)
        return QRect(-100000, -100000, 200000, 200000)

    def body_rect_global(self):
        """可见气泡本体在屏幕坐标里的矩形（不含透明外圈）。"""
        top_left = self.mapToGlobal(QPoint(self.MARGIN_X, self.MARGIN_TOP))
        return QRect(top_left.x(), top_left.y(), self._body_w, self._body_h)

    def is_flipped(self):
        """True 表示整块翻到了铃铛上方（下方放不下），小三角朝下。"""
        return self._flipped

    def caret_center_global_x(self):
        return self.mapToGlobal(QPoint(0, 0)).x() + self.MARGIN_X + self._caret_x

    def show_below(self, anchor_global, icon_top_y=None):
        """气泡本体水平中心对准铃铛中心，顶边在铃铛图标下方 GAP_TO_ICON。

        anchor_global 是铃铛图标底边中心；icon_top_y 是图标上沿的屏幕 y，只用于下方放不下时
        翻到上方（不传就永不翻转）。靠近屏幕左右边时本体向内收拢，小三角仍留在铃铛正下方/上方，
        只在离本体圆角不足 CARET_INSET 时才被迫偏离。
        """
        area = self._available_rect(anchor_global)
        edge = self.SCREEN_EDGE
        bw, bh = self._body_w, self._body_h
        ax = anchor_global.x()
        left = int(round(ax - bw / 2.0))
        left = max(area.left() + edge, min(left, area.right() + 1 - edge - bw))
        top = anchor_global.y() + self.GAP_TO_ICON
        flipped = False
        if icon_top_y is not None and top + bh > area.bottom() + 1 - edge:
            above = icon_top_y - self.GAP_TO_ICON - bh
            if above >= area.top() + edge:
                flipped, top = True, above
        self._flipped = flipped
        self._caret_x = float(max(self.CARET_INSET, min(ax - left, bw - self.CARET_INSET)))
        target = QPoint(left - self.MARGIN_X, int(top) - self.MARGIN_TOP)
        if self.isVisible() and self.pos() == target:
            # 已经开着、位置也没变（行情只是把数字换了）：只重绘，不再移动、不再重新显示与置顶。
            # 探针实测：活跃行情下每秒 6~7 次文字变化，旧写法每次都走一遍 move+show+raise_。
            self.update()
            return
        self.move(target)
        self.update()
        self.show()
        self.raise_()

    def _shape_path(self):
        """本体圆角矩形 + 指向铃铛的小三角，合成一条轮廓（描边时三角与本体之间没有接缝）。"""
        w, h, c = float(self._body_w), float(self._body_h), self._caret_x
        body = smooth_round_rect_path(0, 0, w, h, self.RADIUS)
        k = self.CARET_H
        if self._flipped:
            tri = QPolygonF([QPointF(c - k - 1, h - 1), QPointF(c, h + k), QPointF(c + k + 1, h - 1)])
        else:
            tri = QPolygonF([QPointF(c - k - 1, 1), QPointF(c, -k), QPointF(c + k + 1, 1)])
        caret = QPainterPath()
        caret.addPolygon(tri)
        caret.closeSubpath()
        return body.united(caret)

    def _paint_shadow(self, p):
        """两层柔影（贴边收口 + 向下散开）。Qt 没有现成模糊，用一组逐层外扩、alpha 递减的圆角矩形逼近高斯。"""
        w, h = float(self._body_w), float(self._body_h)
        # (纵向偏移, 高斯 σ, 最深处 alpha 0~1)，对应效果图 0 1px 2px / 0 8px 20px
        for dy, sigma, peak in ((1.0, 1.0, 0.28), (8.0, 10.0, 0.30)):
            prev = 0.0                                  # 已叠在外侧的累计覆盖
            for i in range(8, -9, -1):                  # 从最外层往里：+2σ … -2σ，每 σ/4 一层
                d = i * sigma / 4.0
                # 「刚好在这一层边缘内侧」那一带应有的累计覆盖；越往里越深，所以逐层递增
                cum = peak * 0.5 * math.erfc((d - sigma / 8.0) / (sigma * math.sqrt(2.0)))
                # 累计覆盖逐层严格递增且恒小于 peak（< 1），这一层的 alpha 因此恒为正，不需要再防 0
                layer = 1.0 - (1.0 - cum) / (1.0 - prev)
                prev = cum
                rect = QRectF(-d, dy - d, w + 2 * d, h + 2 * d)
                if rect.width() <= 0 or rect.height() <= 0:
                    continue
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(0, 0, 0, max(1, int(round(layer * 255)))))
                radius = max(0.0, self.RADIUS + d)
                p.drawRoundedRect(rect, radius, radius)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.translate(self.MARGIN_X, self.MARGIN_TOP)
        self._paint_shadow(p)
        shape = self._shape_path()
        p.fillPath(shape, self.BODY_COLOR)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(self.RING_COLOR, 1))
        p.drawPath(shape)
        self._paint_rows(p)
        p.end()

    def _paint_rows(self, p):
        """组标题 + 每行三列。三列共用同一条文字基线（取价位字体的度量，方向词与百分比跟着它），
        方向词再上提 WORD_RAISE 抵掉汉字笔画的下探，使三列可见底边齐平；标题圆点底边与标题文字底边齐平。"""
        price_f, pct_f, word_f, header_f = self._fonts()
        word_w, price_w, pct_w = self._columns()
        hfm, pfm = QFontMetrics(header_f), QFontMetrics(price_f)
        x_word = float(self.PAD_X)
        x_price = x_word + word_w + self.COL_GAP
        x_pct_right = x_price + price_w + self.COL_GAP + self.PCT_GAP + pct_w
        row_off = (self.ROW_H - (pfm.ascent() + pfm.descent())) / 2.0 + pfm.ascent()
        head_off = (self.HEADER_H - (hfm.ascent() + hfm.descent())) / 2.0 + hfm.ascent()
        y = float(self.PAD_TOP)
        groups = [(k, t, dot) for k, t, dot in (
            ("pending", "待触发", self.PENDING_DOT_COLOR), ("hit", "已触发", self.HIT_DOT_COLOR))
            if self._rows[k]]
        for gi, (key, title, dot) in enumerate(groups):
            base = y + head_off
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(dot)
            # 圆点底边与标题文字的可见底边齐平（文字底边 = 基线 + HEADER_INK_OVER）。
            # 标题基线常落在半像素上（实测 20.5）：2 倍屏上刚好是设备像素边界，1 倍屏上文字会被取整而圆点
            # 按半像素抗锯齿，差出 1px，所以圆点底边按所在设备的像素向上取整。
            # 缩放取两处的较大值：直接画到窗口时 device() 报真实倍率；render() 到 2 倍图片时 device() 仍是
            # 窗口本身（实测报 1.0），真实缩放只在 deviceTransform() 里（报 2.0）。
            dpr = max(1.0, p.device().devicePixelRatioF(), p.deviceTransform().m11())
            dot_bottom = math.ceil((base + self.HEADER_INK_OVER) * dpr - 1e-6) / dpr
            p.drawEllipse(QRectF(self.PAD_X, dot_bottom - self.DOT_SIZE, self.DOT_SIZE, self.DOT_SIZE))
            p.setFont(header_f)
            p.setPen(self.HEADER_COLOR)
            p.drawText(QPointF(self.PAD_X + 12, base), title)
            y += self.HEADER_H + self.HEADER_GAP
            for is_up, price_text, pct_text in self._rows[key]:
                base = y + row_off
                if key == "hit":
                    color = self.HIT_TEXT_COLOR
                else:
                    color = self.UP_COLOR if is_up else self.DOWN_COLOR
                p.setPen(color)
                p.setFont(word_f)
                p.drawText(QPointF(x_word, base - self.WORD_RAISE), "涨到" if is_up else "跌到")
                p.setFont(price_f)
                p.drawText(QPointF(x_price, base), price_text)
                if pct_text:
                    p.setFont(pct_f)
                    adv = QFontMetrics(pct_f).horizontalAdvance(pct_text)
                    p.drawText(QPointF(x_pct_right - adv, base), pct_text)
                y += self.ROW_H
            if gi < len(groups) - 1:
                y += self.SEP_GAP_TOP
                p.setPen(QPen(self.SEP_COLOR, 1))
                p.drawLine(QPointF(self.PAD_X, y + 0.5), QPointF(self._body_w - self.PAD_X, y + 0.5))
                y += 1 + self.SEP_GAP_BOTTOM


class ToolWindowManager:
    """主窗持有的工具窗引用表。

    编辑器、目标价面板和右键菜单仍由各自的现有代码创建与销毁；本对象只拥有
    引用，避免这些生命周期字段继续散落在主窗的业务状态中。FloatWindow 保留
    同名 property，兼容旧测试和历史调用方。
    """

    def __init__(self):
        self.editor = None
        self.context_menu = None
        self.target_panel = None
        self.target_bubble = None

    def clear(self) -> None:
        self.editor = None
        self.context_menu = None
        self.target_panel = None
        self.target_bubble = None


def _flush_deferred_deletions():
    """在进程退出前，把排队待删（deleteLater）的 Qt 对象就地销毁。

    为什么必须显式做（v1.4.20，修 iMac 上的退出 SIGSEGV）：macOS 上
    QApplication.quit() **不是**"让 app.exec() 返回"，Qt 的 cocoa 插件会把它
    转成 -[NSApplication terminate:] -> exit()（本机复现栈与线上崩溃报告逐帧
    一致：meth_QCoreApplication_quit → libqcocoa → -[NSApplication terminate:]
    → exit → __cxa_finalize_ranges）。进程因此不回 Python 侧收尾，直接跑 C++
    静态析构；Qt 到析构阶段才去冲刷排队的 DeferredDelete，而那时主线程的
    QThreadStorage 已被更早的 atexit 例程销毁 —— 销毁任何带 QWindow 的 widget
    就会走 QSurface::~QSurface → QOpenGLContext::currentContext →
    QThreadStorageData::get() 空指针。崩溃报告的特征就是这三帧。

    提前到本函数冲刷时，Qt 与 AppKit 都还完整，析构走正常路径。
    只在主线程执行：工作线程没有 Qt 事件循环，替别人冲刷排队事件是错的。

    返回 True 表示确实执行了一次冲刷（供调用方与用例判断，不作为行为依据）。
    """
    if threading.current_thread() is not threading.main_thread():
        return False
    try:
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        return True
    except Exception as exc:
        # 冲刷失败不能阻断退出：宁可保留原行为，也不能让用户退不掉程序。
        log_line("flush deferred deletes failed: %r" % (exc,), "WARNING")
        return False


class FloatWindow(GlassMixin, RenderMixin, QWidget):
    """A股实时盯盘悬浮窗主窗口。

    v1.2.40（上帝类拆分第二步）：本类历史来自多版本功能堆积，事件/线程/轮询/动画/
    状态/UI 高度耦合，改一处易牵扯别处。按三方向分版迁移（规划见 sf_scheduler.py
    模块 docstring）：
      ① 动画状态机（价格/涨跌幅插值、收展布局插值、跌破整数关口判定、微小变动/
         动画时长决策）→ sf_animator.py
         （v1.2.40 已下沉纯逻辑，主窗保留 _disp_*/_anim_* 状态字段与 QTimer/重绘，
         本类仅薄委托）
      ② 行情循环与轮询调度（熔断状态机 POLL_*、UI 轮询间隔决策、分时快速重试窗口、
         行情/分时线程的"到期拉取/长暂停退避"调度骨架）→ sf_quote_loop.py
         （v1.2.40 已下沉；v1.2.39 的熔断→间隔映射在 sf_scheduler.py）
      ③ 运行时调度（App Nap/置顶/省电）→ sf_runtime.py
         （v1.2.41 已下沉 NapTokenManager（App Nap 令牌生命周期）/ RuntimeState
         （缩 Dock↔恢复门控）/ restore_ui_interval（恢复窗口轮询间隔决策），
         本类保留 _minimized property 与 _update_nap_policy 薄委托）
    结论：三方向拆分完毕，本类仅剩 UI 渲染 / 事件 / 线程编排与薄委托。
    拆分原则：纯逻辑抽离、主窗保薄委托、对外行为 100% 不变、单测全绿后出包。
    """

    # v1.4.10 功耗：数据到达唤醒信号。
    # 行情/分时/更新回包线程把数据放进 self.q 之后 emit 一次，主线程经**队列连接**
    # 在自己的事件循环里执行 _poll。取代原来"每 16ms 定时去排一次队列"——实测那样
    # 每秒醒 62.3 次，其中约 84% 队列是空的。改后唤醒次数与数据条数同级（≈10/秒），
    # 且端到端等待窗口从 0~16ms 压到 0~2ms：更省电，也更跟手。
    # 必须显式 Qt.QueuedConnection：见 __init__ 里的连接处注释。
    _prefer_liquid_glass = True
    ui_wakeup = pyqtSignal()

    # 人脸守卫：采集线程数完一张新快照就 emit 一次，主线程立刻判一遍。
    # 为什么必须有它：迟滞在 2026-10-12 起按样本时刻去重（同一份快照不再喂两次），
    # 于是"两次命中"真的需要两次采样，判定再靠轮询对齐就会平白多等半个节拍。
    # 与 ui_wakeup 同理：**必须显式 QueuedConnection** —— 发出信号的是 dispatch
    # 原生线程，AutoConnection 会判成同线程而走 DirectConnection，那样
    # `_on_face_guard_tick` 就在采集线程上跑，等于跨线程碰 QWidget（会画花甚至崩）。
    face_guard_wakeup = pyqtSignal()
    diagnostics_ready = pyqtSignal(object)
    maintenance_wakeup = pyqtSignal()

    # 收展动画期间的重复整窗帧合并：resizeEvent 每次都会排一次整窗帧，并把序号加一；
    # 呼吸灯时钟自己再排一次就是同一个 vsync 周期里的第二帧（实测展开时约 30% 的相邻
    # 绘制间隔小于 8ms，屏幕只有 60Hz，多出来的帧根本不会被显示）。
    # 放在类上而不是 __init__：绕过构造的测试替身也读得到默认值。
    _resize_paint_seq = 0
    _dot_seen_resize_seq = 0

    # v1.2.41：_minimized 状态下沉 sf_runtime.RuntimeState，property 保持旧字段名兼容
    @property
    def _minimized(self):
        return self._runtime.minimized

    @_minimized.setter
    def _minimized(self, v):
        self._runtime.minimized = v

    # v1.2.41：_nap_activity 令牌下沉 sf_runtime.NapTokenManager，property 保持旧字段名兼容
    @property
    def _nap_activity(self):
        return self._runtime.nap_token

    @property
    def running(self):
        return _lazy_attr(self, "_lifecycle_controller", WindowLifecycleController).running

    @running.setter
    def running(self, value):
        _lazy_attr(self, "_lifecycle_controller", WindowLifecycleController).running = bool(value)

    @property
    def _quitting(self):
        return _lazy_attr(self, "_lifecycle_controller", WindowLifecycleController).quitting

    @_quitting.setter
    def _quitting(self, value):
        controller = _lazy_attr(self, "_lifecycle_controller", WindowLifecycleController)
        controller.state.quitting = bool(value)

    @property
    def _threads_stopped(self):
        return _lazy_attr(self, "_lifecycle_controller", WindowLifecycleController).threads_stopped

    @_threads_stopped.setter
    def _threads_stopped(self, value):
        controller = _lazy_attr(self, "_lifecycle_controller", WindowLifecycleController)
        controller.state.threads_stopped = bool(value)

    @property
    def quote(self):
        return _lazy_attr(self, "_quote_presentation", QuotePresentationController).state.quote

    @quote.setter
    def quote(self, value):
        _lazy_attr(self, "_quote_presentation", QuotePresentationController).state.quote = value

    @property
    def minute_points(self):
        return _lazy_attr(self, "_quote_presentation", QuotePresentationController).state.minute_points

    @minute_points.setter
    def minute_points(self, value):
        _lazy_attr(self, "_quote_presentation", QuotePresentationController).state.minute_points = value

    @staticmethod
    def _state_property(name, state_name=None):
        state_name = state_name or name.lstrip("_")

        def getter(self):
            return getattr(_lazy_attr(
                self, "_quote_presentation", QuotePresentationController).state, state_name)

        def setter(self, value):
            setattr(_lazy_attr(
                self, "_quote_presentation", QuotePresentationController).state, state_name, value)

        return property(getter, setter)

    _minute_day = _state_property("_minute_day")
    _minute_stale = _state_property("_minute_stale")
    _minute_last_ok_at = _state_property("_minute_last_ok_at")
    _net_ok = _state_property("_net_ok")
    _quote_fail = _state_property("_quote_fail")
    _last_ok_time = _state_property("_last_ok_time")
    _real_price = _state_property("_real_price")
    _price_is_fallback = _state_property("_price_is_fallback")
    _pct_unknown = _state_property("_pct_unknown")
    _disp_price = _state_property("_disp_price")
    _disp_pct = _state_property("_disp_pct")
    _line_ver = _state_property("_line_ver")
    _chart_changed = _state_property("_chart_changed")
    _need_redraw = _state_property("_need_redraw")
    _width_dirty = _state_property("_width_dirty")

    @staticmethod
    def _update_state_property(name, state_name=None):
        state_name = state_name or name.lstrip("_")

        def getter(self):
            return getattr(_lazy_attr(self, "_update_state", UpdateFlowState), state_name)

        def setter(self, value):
            setattr(_lazy_attr(self, "_update_state", UpdateFlowState), state_name, value)

        return property(getter, setter)

    _update_checking = _update_state_property("_update_checking")
    _update_downloading = _update_state_property("_update_downloading")
    _update_prompted_version = _update_state_property("_update_prompted_version")
    _update_pending = _update_state_property("_update_pending")
    _update_cancel = _update_state_property("_update_cancel")
    _update_prompt_when_ready = _update_state_property("_update_prompt_when_ready")
    _update_ready_archive = _update_state_property("_update_ready_archive")
    _update_ready_version = _update_state_property("_update_ready_version")
    _update_progress_pending = _update_state_property("_update_progress_pending")
    _update_manual_wait = _update_state_property("_update_manual_wait")
    _update_last_check_at = _update_state_property("_update_last_check_at")
    _update_last_result = _update_state_property("_update_last_result")
    _update_stale_resets = _update_state_property("_update_stale_resets")
    _update_check_aborted = _update_state_property("_update_check_aborted")
    _update_failure_count = _update_state_property("_update_failure_count")
    _update_transition_pending = _update_state_property("_update_transition_pending")

    @staticmethod
    def _target_state_property(name, state_name=None):
        state_name = state_name or name.lstrip("_")

        def getter(self):
            return getattr(_lazy_attr(self, "_target_controller", TargetController).state, state_name)

        def setter(self, value):
            setattr(_lazy_attr(self, "_target_controller", TargetController).state, state_name, value)

        return property(getter, setter)

    _alerts = _target_state_property("_alerts")

    @staticmethod
    def _face_value_property(name, state_name=None):
        state_name = state_name or name.removeprefix("_face_guard_")

        def getter(self):
            return getattr(_lazy_attr(
                self, "_face_guard_controller", FaceGuardController).state, state_name)

        def setter(self, value):
            setattr(_lazy_attr(
                self, "_face_guard_controller", FaceGuardController).state, state_name, value)

        return property(getter, setter)

    _face_guard_pref = _face_value_property("_face_guard_pref", "preference")
    _face_guard_paused = _face_value_property("_face_guard_paused")
    _face_guard_next_retry = _face_value_property("_face_guard_next_retry")
    _face_guard_logged_state = _face_value_property("_face_guard_logged_state")
    _face_guard_stalled = _face_value_property("_face_guard_stalled")
    _face_guard_screen_locked = _face_value_property("_face_guard_screen_locked")
    _face_guard_screen_probe_at = _face_value_property("_face_guard_screen_probe_at")
    _face_guard_last_fresh = _face_value_property("_face_guard_last_fresh")
    _face_guard_fed_ts = _face_value_property("_face_guard_fed_ts")
    _face_guard_last_stop = _face_value_property("_face_guard_last_stop")

    @staticmethod
    def _face_session_property(name):
        def getter(self):
            return getattr(_lazy_attr(
                self, "_face_guard_controller", FaceGuardController), name)

        def setter(self, value):
            setattr(_lazy_attr(
                self, "_face_guard_controller", FaceGuardController), name, value)

        return property(getter, setter)

    _face_guard_monitor = _face_session_property("monitor")
    _face_guard_arbiter = _face_session_property("arbiter")
    _face_guard_pending_stop = _face_session_property("pending_stop")

    @property
    def _diagnostics_exporting(self):
        return self._diagnostics_controller.exporting

    @_diagnostics_exporting.setter
    def _diagnostics_exporting(self, value):
        self._diagnostics_controller.exporting = value

    @property
    def _diagnostics_thread(self):
        return self._diagnostics_controller.thread

    @_diagnostics_thread.setter
    def _diagnostics_thread(self, value):
        self._diagnostics_controller.thread = value

    @property
    def _quote_thread(self):
        return self._quote_runtime.quote_thread

    @_quote_thread.setter
    def _quote_thread(self, value):
        self._quote_runtime.replace_quote(value)

    @property
    def _minute_thread(self):
        return self._quote_runtime.minute_thread

    @_minute_thread.setter
    def _minute_thread(self, value):
        self._quote_runtime.replace_minute(value)

    @property
    def _editor(self):
        return self._tool_windows.editor

    @_editor.setter
    def _editor(self, value):
        self._tool_windows.editor = value

    @property
    def _ctx_menu(self):
        return self._tool_windows.context_menu

    @_ctx_menu.setter
    def _ctx_menu(self, value):
        self._tool_windows.context_menu = value

    @property
    def _target_panel(self):
        return self._tool_windows.target_panel

    @_target_panel.setter
    def _target_panel(self, value):
        self._tool_windows.target_panel = value

    @property
    def _target_bubble(self):
        return self._tool_windows.target_bubble

    @_target_bubble.setter
    def _target_bubble(self, value):
        self._tool_windows.target_bubble = value

    def __init__(self):
        super().__init__()
        self._tool_windows = ToolWindowManager()
        self._lifecycle_controller = WindowLifecycleController()
        self._lifecycle_state = self._lifecycle_controller.state
        self._quote_presentation = QuotePresentationController()
        self._quote_state = self._quote_presentation.state
        self._update_state = UpdateFlowState()
        self._target_controller = TargetController()
        self._target_state = self._target_controller.state
        self._face_guard_controller = FaceGuardController()
        self._face_guard_values = self._face_guard_controller.state
        # 目标价与替换股票均为可独立关闭的工具窗；禁止 Qt 把它们关闭误判为
        # “最后一个窗口已关闭”而退出整个行情应用。放在主窗构造期，开发态与打包态一致。
        app = QApplication.instance()
        if app is not None:
            app.setQuitOnLastWindowClosed(False)
        self.code, x, y, h0, self._bg_mode = load_config()
        # 1.3 v1.2.0：启动时从 config.json 加载 API 地址覆盖（改地址无需重新打包）
        load_api_urls()
        self.q = queue.Queue(maxsize=32)  # 有界队列：UI 卡顿时丢弃最旧行情，防止内存无限积压
        self._update_controller = UpdateController(
            lambda item: self._q_put(item), _bundle_version)
        self._diagnostics_controller = DiagnosticsExportController(_publish_diagnostics_file)
        self._quote_runtime = QuoteRuntimeController()
        self._target_ticks = TargetTickBuffer()
        self._snapshot_writer = SnapshotWriter(save_last_quote)
        self.running = True
        self._editor = None
        self._ctx_menu = None
        self._target_bubble = TargetBubble()
        # 铃铛气泡的悬停节拍：两张单次定时器，随主窗销毁（父对象是 self）。
        self._bubble_show_timer = QTimer(self)
        self._bubble_show_timer.setSingleShot(True)
        self._bubble_show_timer.timeout.connect(self._on_bubble_show_due)
        self._bubble_hide_timer = QTimer(self)
        self._bubble_hide_timer.setSingleShot(True)
        self._bubble_hide_timer.timeout.connect(self._on_bubble_hide_due)
        self._drag = False
        self._sys_drag = False          # v1.4.29：本手势已交给系统拖拽（开关 system_drag）
        self._offset = QPoint()
        self._resizing = False
        self._resize_hold_until = 0.0   # v1.4.28：拉伸期间合并时钟的保持窗口
        self._resize_start = QPoint()
        self._resize_h0 = self.height()
        self._resize_target_h = None   # v1.2.28：拉伸帧合并目标高度（16ms 定时器单帧应用）
        self._hide_dock_timer = None
        # v1.2.41：运行时节电/缩 Dock 状态（App Nap 令牌 + 最小化门控）下沉 sf_runtime.RuntimeState
        self._runtime = RuntimeState()
        # 渲染缓存：背景路径 / 分时折线路径，尺寸或数据未变化时复用，降低重绘开销
        self._bg_path = None
        self._bg_path_wh = None
        self._bg_path_ins = None   # glass 模式内缩填充路径（裁描边）
        self._bg_path_ins_wh = None  # 内缩路径缓存键（独立于 _bg_path_wh，避免窗口加宽后不复建）
        self._line_path = None
        self._line_path_key = None
        self._line_ver = 0   # 分时数据版本号：每次整表替换 +1，驱动折线缓存失效（替代 id(pts)）
        # v1.2.20：分时坐标缓存（x 绝对坐标 + y 归一化数组），拉伸高度变化时
        # 仅 y 缩放即可，避免每帧 240 点 hhmm 解析与乘除重算
        self._chart_cache_key = None
        self._chart_cache_xs = None
        self._chart_cache_np = None   # 价格归一化 (hi-price)/(hi-lo)
        self._chart_cache_na = None   # 均价归一化
        # 毛玻璃（NSVisualEffectView）状态
        self._bg_effect = False    # 是否已生效系统毛玻璃
        self._glass_view = None
        self._glass_window = None   # 独立毛玻璃 NSWindow（置于主窗下层）
        self._glass_mask_layer = None  # 玻璃层圆角裁剪 CAShapeLayer（随尺寸重建）
        self._glass_content = None
        self._last_glass_mask_ts = 0.0  # v1.2.20：玻璃层 mask 最近重建时刻（拉伸节流用）
        # 并发防重入：网络请求未完成时跳过新一轮，避免慢网络下线程无限堆积
        self._quote_lock = threading.Lock()
        # ⚠ 两把锁管两件不同的事，别再合并（修的是实测 33.78s 的主线程冻结）：
        #   _minute_busy  只由分时线程**非阻塞**抢，用来挡住"上一请求还在飞"。
        #                 它会被攥在整个网络调用期间 —— 实测两个源都卡死时是 34.3s。
        #   _minute_lock  只保护 minute_points 的读写，全部是主线程上的微秒级临界区。
        # 合成一把的后果是看得见的：主线程 _maybe_cache_first_screen 做**阻塞** acquire，
        # 于是每 20 秒（首屏快照的写入节流）撞上分时线程攥着锁跑网络，整条 Qt 事件循环
        # 停在 _apply_quote 里 —— 不重绘、不响应点击，而且**同一批队列里已经到手的分时
        # 数据要等它解锁才贴得上屏**，"数据暂缺 · 重试中"因此在数据早就回来之后还挂着。
        # minute_points 的写入方（_apply_minute_points / _handle_minute_stale）全部只在
        # 主线程，彼此不可能并发，所以这里刻意不给它们加锁：加了不解决任何竞态，
        # 只多一处死锁面。判据 tests/test_minute_recovery_latency.py::GuardIsNotTheDataLock
        self._minute_busy = threading.Lock()
        self._minute_lock = threading.Lock()
        # 顶部按钮交互状态（hover 高亮 + 按下/松开必须同一按钮才触发）
        self._hover_btn = None   # "edit"/"min"/"close"/None
        self._press_btn = None   # 按下时的按钮
        self._press_chart = False  # v1.2.6：按下位置是否在分时图热区内（按下与松开均在热区内时切换展开/收起）
        self.quote = None
        self.minute_points = []
        # v1.4.7 分时数据容错：记录当前分时曲线所属交易日 + 是否处于"重连中"。
        # 盘中网络抖动时不再清空当日曲线（避免闪回"加载中"死白屏），而是保留
        # 最后有效走势并置 _minute_stale，由渲染层显示顶部重试标签；
        # 一旦重连成功即自动清除。跨日数据仍会被清空，绝不冒充当日走势。
        self._minute_day = ""       # 当前 minute_points 所属北京时间日；"" = 未知
        self._minute_stale = False  # True = 当日曲线已滞后，正在后台重连
        self._minute_last_ok_at = 0.0  # 最后一次写入有效分时点的墙钟（诊断算数据龄用）
        # 诊断留痕（S52 那一族的现场）：分时快速重试窗口的状态此前是 _minute_loop
        # 的局部量，诊断包只能看到 minute_failures，答不出"现在是不是停在重试中、
        # 还剩几次"。由 _minute_loop 每次状态转移后写回，主线程只读。
        self._minute_retry_left = 0
        self._minute_retry_hot = False
        # v6：「目标价」实时列表（runtime 态，不持久化 state/last_side）
        self._alerts = []
        # 数字跳动动效：价格/涨跌幅从旧值平滑滚动到新值
        self._disp_price = None      # 当前显示价格（动画插值）
        self._disp_pct = None        # 当前显示涨跌幅（动画插值）
        # v1.3.49：涨跌幅「未知」标记 —— 主源缺 pct 且昨收也不可用（无法自算）时为
        # True，渲染层显示 "--"（中性灰）而不是把兜底 0.0 渲染成带涨跌色的 "0.00%"。
        # 注意 _disp_pct 恒为数值（参与动画算术），真值语义由本标记承载。
        self._pct_unknown = False
        # v1.2.57：分时图末端圆点改为持续呼吸灯效果（渲染层内置 sin 周期），
        # 不再需要 _dot_flash_until 门控变量。
        # v1.2.48：动画驱动状态（价格滚动/破位白闪/下跌红闪/布局动画/目标价彩虹）下沉
        # sf_anim_driver.AnimDriver —— 默认值在模块构造内初始化（逐位一致），
        # 下方经 property（_anim_*/_price_flash_*/_flash_drop_*/_layout_anim_*）
        # 读写委托到 driver 字段，主窗其余代码零改动、对外行为 100% 不变。
        self._anim_driver = AnimDriver()
        self._stock_transition = StockTransition(self)
        self._anim_timer = QTimer(self)
        self._anim_timer.timeout.connect(self._anim_tick)
        self._anim_timer.setInterval(16)
        # v1.2.65 第1轮走查修复：Cmd+Q / Dock「退出」在 macOS 上走
        # QApplication::quit()，它**不会**向窗口投递 closeEvent（窗口没被关，
        # 只是事件循环退出）。此前没有任何钩子接管这条路径 → 窗口位置与拉伸
        # 高度全部不写盘，下次启动回到上次「点过关闭按钮」时的位置，用户手动
        # 摆好的位置每次都要重摆。aboutToQuit 是这条路径唯一的通知点。
        # 注意：连接以 self 为接收者，窗口销毁时 PyQt 会自动断开，不会泄漏。
        try:
            app = QApplication.instance()
            if app is not None:
                # v1.4.33：先落"最近静默失效"摘要，再做配置持久化 ——
                # 摘要平时按 5 秒限流写盘，退出这一笔补齐，保证次数不说假话。
                app.aboutToQuit.connect(flush_last_failures_now)
                app.aboutToQuit.connect(self._persist_on_exit)
                # v1.2.95：Cmd+Q 也完全绕过了 quit() 里的线程清理，三条 daemon
                # 循环与编辑器 SearchThread 无人停止。Qt 按连接顺序调用，先
                # 持久化再停线程（后者需要窗口仍可用于取 _editor）。
                app.aboutToQuit.connect(self._shutdown_threads)
        except Exception as exc:
            log_line("aboutToQuit hook failed: %r" % (exc,))
        # v1.2.43 第1项：动画定时器设为精确模式 PreciseTimer —— 默认 CoarseTimer(5% 容差)
        # 在 120Hz 高刷屏会周期性吞帧（16ms 周期实际抖动到 ~20ms），精确定时消除每帧
        # 抖动，价格滚动/收展动画在 120Hz 下逐帧稳定。
        self._anim_timer.setTimerType(Qt.TimerType.PreciseTimer)
        # 价格跌破整数关口闪烁（如 404→399.99 破 400）：定时器驱动高亮交替
        # （相位/截止时刻状态已下沉 AnimDriver，见 _anim_driver）
        self._flash_timer = QTimer(self)
        self._flash_timer.timeout.connect(self._flash_tick)
        self._flash_timer.setInterval(FLASH_IVAL)
        self._flash_timer.setTimerType(Qt.TimerType.PreciseTimer)  # v1.2.43 第1项：破位白闪精确定时
        # 12.x v1.2.11 下跌闪烁：各档独立冷却记录 + 独立相位定时器（不阻塞行情刷新）
        # 冷却/相位状态已下沉 AnimDriver 构造默认值（flash_drop_*），此处仅保留定时器
        self._flash_drop_timer = QTimer(self)
        self._flash_drop_timer.timeout.connect(self._drop_flash_tick)
        self._flash_drop_timer.setInterval(FLASH_DROP_IVAL_MS)
        self._flash_drop_timer.setTimerType(Qt.TimerType.PreciseTimer)  # v1.2.43 第1项：下跌红闪精确定时
        # 目标价命中后的流动彩虹：约 30fps，只重绘价格大字所在主窗。
        self._rainbow_timer = QTimer(self)
        self._rainbow_timer.timeout.connect(self._rainbow_tick)
        # v1.4.14 帧率优先：33ms(30 帧/秒) → 16ms。彩虹是 6 秒两轮的连续色带扫动，
        # 30 帧/秒在 60Hz 屏上每帧要停留两个 vsync，看得出阶跃。老板要求所有连续动效
        # 稳定 60 帧以上，故与价格滚动同频。（相位交替的白闪/红闪不改：见下方注释。）
        self._rainbow_timer.setInterval(16)
        self._rainbow_timer.setTimerType(Qt.TimerType.PreciseTimer)
        # v1.4.14 帧率优先：分时图末端**呼吸灯专属时钟**。
        #
        # 病根：呼吸灯（_draw_chart_dot）的相位按墙上时钟算，但它没有自己的定时器，
        # 只在**整窗重绘**时被顺带画一次。价格一跳就有整窗帧，所以盘中"看起来"在呼吸；
        # 一旦行情平静、价格不变，实测整窗帧只有 0.2 次/秒 —— 那个"实时"暗示其实是冻住的。
        # 这是 v1.4.14 帧率审计量出来的、先于本轮功耗优化就存在的设计缺口。
        #
        # 现在给它 16ms 独立时钟（与价格滚动同频，60Hz 屏上正好一帧一跳）。
        # 定时器**不常驻**：收起图表、无数据、滞后冻结、缩到 Dock、窗口不可见时自行停表，
        # 由 _sync_dot_timer() 在这些状态变化处重新武装 —— 停表不影响任何可见帧率，
        # 因为那些时刻本来就没有动画。
        self._dot_timer = QTimer(self)
        self._dot_timer.setInterval(16)
        self._dot_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._dot_timer.timeout.connect(self._dot_tick)
        # v6：专用清空定时器（D1）：按 session_date 15:00:30 单发触发，到点静默清空
        self._clear_timer = QTimer(self)
        self._clear_timer.setSingleShot(True)
        self._clear_timer.timeout.connect(self._clear_expired)
        # 顶部三个按钮图标：v1.4.11 起在**首帧之前**同步渲染（约 5ms）。
        # 此前由后台线程投递，首帧只能画自绘兜底造型（edit/close 墨迹 13px、
        # 且是铅笔不是齿轮），SVG 到位后同一位置缩成 8px 的正式造型 —— 用户看到的
        # 就是"启动时右上角按钮突然放大再缩小"。应用图标仍走后台（512px + AppKit 预热，
        # 才是真正需要异步的那一个）。
        self.app_icon = None
        self.app_icon_dark = None     # 深色外观那一张；后台出图，和浅色一起到位
        self._icon_shown = None       # 当前已经贴上的那张，避免外观没变时重复设置
        # 系统在浅色/深色外观间切换时换图标。绑定方法做槽：窗口销毁时 Qt 自动断开。
        connect_appearance_changed(self._on_appearance_changed)
        _btn_pm = button_pixmaps()
        self._edit_pm = _btn_pm["edit"]
        self._close_pm = _btn_pm["close"]
        self._min_pm = _btn_pm["min"]
        self._dock_applied = False
        self._clamped = False
        self._quitting = False
        # v1.2.95：后台线程清理的幂等闸门（独立于 _quitting —— quit() 会先置
        # _quitting 再调 _shutdown_threads，复用该标记会让清理在 quit() 路径上
        # 直接短路，线程一个都 join 不到）
        self._threads_stopped = False
        # 睡眠事件：quit 时 set，使后台线程 _sleep 立即退出（消除忙等）
        self._wake = threading.Event()
        # v1.4.10 功耗：调度变更事件。后台线程不再固定 0.1s 醒一次空转，而是睡到
        # "下一次真正可能需要决策"的时刻；期间任何会改变决策的动作（切股、点立即
        # 刷新、熔断降/升档、缩 Dock/恢复、展开/收起图表）都必须 set 本事件把它
        # 立刻叫醒，否则按需休眠会把"强制刷新"最坏拖到一个完整睡眠周期之后。
        self._resched = threading.Event()
        # Each worker owns its wakeup: clearing a shared Event can lose the other
        # worker's notification if that worker is still finishing a request.
        self._quote_resched = threading.Event()
        self._minute_resched = threading.Event()
        # v1.2.71：立即拉取标志（切股 / 手动刷新 / Dock 恢复）。置位后由常驻
        # 行情线程与分时线程被叫醒后消费（在途请求先完成），取代原先"每次调用新建
        # 两条线程"的做法 —— 单次拉取最坏阻塞约 35s，连续触发会无上限堆积线程，
        # 而新建的线程仍会被防重入锁挡掉，实际拿不到任何额外收益。
        self._force_quote = False
        self._force_minute = False
        self._force_lock = threading.Lock()
        # v1.2.41：App Nap 令牌生命周期下沉 sf_runtime.NapTokenManager（初始未持有，读经 property _nap_activity）
        # Chrome 等第三方系统全屏兼容：私有 CGS 调用失败时按间隔重探，不影响公开置顶路径。
        self._cgs_ok = None
        self._cgs_fail = 0
        self._cgs_disabled_at = 0.0
        # 分时拉取连续失败计数（用于日志降噪）
        self._minute_fail = 0
        # QFont/QFontMetrics 缓存：避免 paintEvent 每帧重建（性能）
        self._fonts = {}
        self._fms = {}
        # v1.2.24：价格/涨跌幅逐位滚动状态（由 sf_ui_render._draw_roll_text 驱动）
        # 以 key（"price"/"pct"）隔离，避免两类文本互串触发滚动
        self._roll_prev = {}          # key -> 上一帧显示串
        self._roll_anim = {}          # key -> {pos: [from_ch, to_ch, start_time, dur]}
        self._roll_pending = {}       # key -> 行情更新预置的逐位滚动最终目标串（v1.2.25）
        # 退出时等待中的搜索线程（(editor, thread) 持有引用，finished 后完成收尾）
        self._quit_pending = None
        self._start_ts = time.monotonic()
        # 脏标记：数据未变化时不重绘（静止状态零绘制开销）
        self._need_redraw = False
        # 宽度计算涉及底部文本与字体度量；仅底部展示内容变化后才重新计算。
        self._width_dirty = True
        self._width_content_key = None
        # v1.2.43 第2/4项：价格动画局部重绘状态
        # _partial_price：None=整窗重绘；非 None=(x,y,w,h)=价格/涨跌幅条带局部脏区，
        #                 由 _anim_tick_inner 在 update(区域) 前写入，paintEvent 据此路由。
        # _chart_changed：分时数据/窗口几何变化标记 —— _poll 在 minute_points 整表
        #                 替换时置 True，paintEvent 整窗路径重画图表后复位 False。
        #                 供价格动画 tick 的 should_draw_chart 判定（动画期数据变了也
        #                 必须整窗重画图表一次，保证图表与价格不错帧）。
        self._partial_price = None
        self._chart_changed = False
        # 网络连续失败计数与状态：后台线程写，状态翻转经队列回传主线程
        self._quote_fail = 0
        # 自动更新不进入行情配置：仅用独立队列消息把后台结果带回 GUI 线程。
        self._update_checking = False
        self._update_downloading = False
        self._update_prompted_version = None
        self._update_pending = None
        self._update_cancel = None
        # 自动检查发现新版后，下载完成的已校验 ZIP 先留在这里，等用户决定是否重启。
        self._update_ready_archive = None
        self._update_ready_version = None
        self._update_prompt_when_ready = False
        self._update_progress_pending = None
        self._update_flow_box = None
        self._update_flow_progress = None
        self._update_flow_cancel = None
        # v1.4.6 更新提示改用 macOS 系统弹窗（sf_system_alert）：三个槽位分别
        # 持有"检查中 / 一次性结果 / 新版确认"的后台弹窗句柄，任意时刻至多一个。
        self._update_check_dialog = None
        self._diagnostics_exporting = False
        self._diagnostics_thread: Optional[threading.Thread] = None
        self._update_check_dialog_token = None
        self._update_message_dialog = None
        self._update_prompt_dialog = None
        self._update_transition_pending = False
        # v1.4.43 两个新账本，都只为"点了必须有反应"服务：
        #   _update_manual_wait：这次点击撞上了一个**已经在跑**的检查时记的账 ——
        #     由那次的回包兑现（该弹确认框/该说"已是最新"/该说失败都照弹）。
        #     没有它，右键"检查更新"会在守卫处直接 return，屏幕上从头到尾什么都不出现。
        #   _update_last_check_at：上一次真正发出请求的时刻（墙钟），给激活补查节流用。
        self._update_manual_wait = False
        self._update_last_check_at = 0.0
        # 诊断留痕：上一次检查的末态与"在飞检查过期复位"的次数（1.4.44 那一支
        # 此前只写一行 WARNING）。runtime.json 据此回答"点了没反应/卡在检查中"。
        self._update_last_result = None
        self._update_stale_resets = 0
        # v1.4.44：用户在「正在检查更新」上按了取消 —— 这一次的结果不再弹（状态照常收）。
        self._update_check_aborted = False
        self._update_failure_count = 0
        self._update_retry_timer = None
        self._update_periodic_timer = None
        self._net_ok = True
        # 5.5 v1.2.5：轮询熔断状态机（交易时段生效；非交易固定 60s 低频）
        # v1.2.22：_poll_state/_poll_fail 由 _quote_loop 工作线程与 UI 定时器
        # 交叉读写，加 RLock 消除跨线程无锁读写隐患。
        self._poll_lock = threading.RLock()
        self._poll_state = POLL_NORMAL
        self._poll_fail = 0
        # 每次启动固定展开分时图；运行中仍可手动收起/展开，但不把上次收起状态带到下次启动。
        self._chart_expanded = True
        self._expanded_h = 0            # 运行期记忆展开高度（手动拉伸后不丢失）
        # （布局动画 from/to/start 已下沉 AnimDriver，见 _anim_driver）
        self._minute_interval = CHART_EXPANDED_MINUTE_INTERVAL  # 分时拉取间隔（收起时放宽省电省流）
        # 2.0 v1.2.0：最近一次行情成功更新的时间（HH:MM:SS），断网时名称栏显示
        self._last_ok_time = None

        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow)
        # 弹层（右键菜单 / 目标价面板 / 编辑器 / 目标价气泡）的 Space 补齐，挂在
        # QApplication 的 Show 事件上，一处实现覆盖四类（实测依据见
        # sf_glass.join_active_space 的注释）。为什么写在主窗构造里而不是 main()：
        # 主窗是应用生命周期内必然存在、且必然被建一次的那个对象，判据可以直接
        # 断言"建完主窗，这一条就挂上了"；写在 main() 里则没有任何可调用行为能证明它挂了。
        install_space_join_filter(QApplication.instance())
        self.setMouseTracking(True)  # 启用鼠标追踪，悬停高亮生效
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowTitle(APP_DISPLAY_NAME)
        self._window_actions, self._native_menu_bar = install_window_actions(
            self, native_menu=native_cocoa_active())
        # 背景模式：glass=系统毛玻璃(窗口显示后注入, 失败自动降级) / translucent=半透明黑 / solid=纯黑
        self._bg_cfg = self._bg_mode          # 用户选择的原始模式：保存配置时用，避免被下方渲染模式覆盖
        self._glass_pending = (self._bg_mode == "glass")
        if self._glass_pending:
            self._bg_mode = "translucent"  # 玻璃生效前按半透明渲染；生效后 paintEvent 走 _bg_effect 分支
        # 窗口图标由后台线程生成后经 _apply_icons 设置（冷启动优化，__init__ 不阻塞）
        # 面板高度可拉伸：固定宽度，高度允许拖拽调整（分时图随高度自动变高）
        self.setFixedWidth(W)
        # v1.3.45：先一次性落到最终几何，**再**施加 min/max 约束。
        #
        # 原顺序（setFixedWidth → setMinimumHeight → setMaximumHeight → resize）
        # 实测会让窗口连穿三个尺寸：Qt 默认高度 480（setFixedWidth 后仍是 184x480）
        # → setMaximumHeight(420) 立刻把 480 钳到 420 → resize 才落到 224。
        # 中间态若被系统绘制一次，用户看到的就是窗口"先很高、再被压扁"的变形。
        # 反序后从构造到显示只有一个尺寸，没有任何可绘制的中间态。
        collapsed = self._height_collapsed()
        # 记忆高度低于「收起 + 最小图高」视为无效（多半是收起态高度被写盘），回落默认展开高度
        startup_h = int(h0) if h0 and int(h0) >= collapsed + CHART_MIN_H else collapsed + CHART_H
        self.resize(W, startup_h)
        self.move(x, y)
        self.setMinimumHeight(MIN_H)
        self.setMaximumHeight(MAX_H)
        self._expanded_h = startup_h

        # v1.3.45：首屏预填 —— 用上次退出前的行情填满显示状态，
        # 这样 win.show() 后的第 1 帧就是成品，不再是占位符。
        # 必须在行情线程启动**之前**：否则两条路径会同时写显示状态。
        self._seeded_from_cache = False
        self._seed_first_screen()

        # 5.1 v1.2.27：后台线程保存引用，供 quit() 显式 join 优雅退出；loop 外层
        # wrapper 捕获子线程异常并带频率限制自动恢复（见 _quote_loop_wrapper）。
        self._quote_runtime.start(self._quote_loop_wrapper, self._minute_loop_wrapper)

        self._performance_monitor = PerformanceMonitor(self)
        self._performance_monitor.start()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll)
        # v1.2.43 第1项：设为精确模式 —— CoarseTimer 有 5% 容差，兜底看门狗也要
        # 按点触发，别让"最迟 1 秒消化积压"变成"1.05 秒"。
        self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        # v1.4.10：数据交付改信号驱动后，本定时器退为 1 秒兜底看门狗。
        # 首屏数据不再依赖它 —— 行情线程一入队就 emit ui_wakeup，比原 16ms 更快。
        self.timer.start(UI_WATCHDOG_INTERVAL_MS)
        # 跨线程唤醒：接收者就是 self（GUI 线程对象），但 emit 发生在行情/分时
        # 等 Python 原生线程里。AutoConnection 比较的是"发出信号的线程"与接收者
        # 线程，从原生线程直接 emit 会被判成同线程而走 DirectConnection，
        # _poll 就会在后台线程上跑 —— 那是跨线程操作 QWidget，会画花甚至崩溃。
        # 因此这里必须写死 QueuedConnection：事件投到 self 所在线程的事件循环执行。
        self.ui_wakeup.connect(self._poll, Qt.ConnectionType.QueuedConnection)
        # The completion originates in a Python worker, never run its Qt slot there.
        self.diagnostics_ready.connect(self._on_diagnostics_ready,
                                       Qt.ConnectionType.QueuedConnection)  # type: ignore[call-arg]
        self.maintenance_wakeup.connect(self._check_updates_background,
                                        Qt.ConnectionType.QueuedConnection)  # type: ignore[call-arg]
        # 人脸守卫的"数完就判"通路（同一个跨线程理由，见信号声明处）。
        # 槽函数自带"没开守卫就立刻 return"的前置判断，所以守卫关着时多一次排队
        # 事件也不会有任何动作；采集线程一停，这个 emit 自然就没有了。
        self.face_guard_wakeup.connect(self._on_face_guard_tick,        # noqa: E501
                                       # mypy 的 PyQt6 存根把 connect 写成"不接受参数"，
                                       # 于是这条与上面 ui_wakeup 那行一起报 call-arg
                                       # —— 运行时 API 确实收 Qt.ConnectionType（装机版
                                       # 两年就是这么跑的）。显式 QueuedConnection 不能
                                       # 为了讨好存根去掉：跨线程走 DirectConnection 会
                                       # 在采集线程上动 QWidget。
                                       Qt.ConnectionType.QueuedConnection)  # type: ignore[call-arg]

        # v1.4.10：交易时段翻转点（09:30 开盘 / 11:31 午休 / 13:00 / 15:01 收盘）
        # 用单发定时器精确武装。原来靠 16ms 的 _poll 顺带发现状态变化，_poll 降到
        # 1 秒看门狗后，App Nap 令牌的切换最坏会晚 1 秒 —— 开盘那一秒系统还在按
        # 省电档调度本进程，第一波行情会明显迟滞。这里提前一步精确唤醒。
        self._session_timer = QTimer(self)
        self._session_timer.setSingleShot(True)
        self._session_timer.timeout.connect(self._on_session_boundary)
        self._arm_session_timer()

        self.top_timer = QTimer(self)
        self.top_timer.timeout.connect(self._force_top)
        # 置顶复核是容错看门狗，不参与行情、动画或输入反馈。用粗粒度计时器
        # 允许 macOS 合并唤醒，减少 WindowServer 线程被无意义唤醒的次数；
        # 间隔仍保持 250ms，外部全屏/Space 切换的恢复上限不变。
        self.top_timer.setTimerType(Qt.TimerType.CoarseTimer)
        self.top_timer.start(250)  # 250ms 复核：浏览器切全屏后亚秒级恢复置顶
        QTimer.singleShot(300, self._force_top)

        # 点击 Dock 图标恢复悬浮窗（最小化到 Dock 后）
        self._setup_reopen_handler()
        QApplication.instance().applicationStateChanged.connect(self._on_app_state)
        QApplication.instance().applicationStateChanged.connect(self._check_updates_after_resume)
        self._clamp_to_screen()
        # 屏幕热插拔、分辨率和缩放变化：窗口可能被甩出可见区域，均需重新钳制。
        # v1.2.72 第6轮走查：此前只监听新增/移除/主屏切换；副屏仍连接但用户
        # 改分辨率、缩放或可用区域时不会触发，窗口可能永久留在屏幕外。
        try:
            qapp = QApplication.instance()
            qapp.primaryScreenChanged.connect(self._on_screen_changed)
            qapp.screenAdded.connect(self._on_screen_added)
            qapp.screenRemoved.connect(self._on_screen_changed)
            for screen in qapp.screens():
                self._watch_screen_geometry(screen)
        except Exception:
            pass

        # v6：启动载入目标价（过期丢弃 / 越线补提醒 / 武装清空定时器）
        self._init_targets()
        # ---- v1.4.40 人脸在场守卫 ----
        # 默认关闭：要摄像头权限的功能不能替用户默认打开（且一开就亮绿灯、不可隐藏）。
        # 这里只登记状态与一张轮询表，**不**拉起采集 —— 采集由 _sync_face_guard
        # 按"窗口可见 / 是否因第二张脸被收起"决定，避免在 __init__ 里抢在首帧之前
        # 弹系统权限框。
        self._face_guard_pref = load_face_guard_pref()
        self._face_guard_monitor = None
        # v1.4.59（S1 第二处）：`stop(wait=False)` 返回 False 的含义是"**还没停**"，
        # 补停跑在 daemon 线程上，进程一退就被硬杀 → 会话永远没停、绿灯不灭，
        # 而诊断包会报 camera_held=false（说谎）。所以这类未完成的停必须留个名字，
        # 退出收尾时逐个 stop(wait=True) 兑现。
        self._face_guard_pending_stop = []
        # v1.4.54：菜单开着时刷新勾号的定时器与动作引用。显式注解 Optional ——
        # mypy 会从「先赋 QTimer/QAction、后赋 None」推出两个互相矛盾的属性类型
        # （本轮被静态闸门抓到 assignment 9→11，注解是正解而不是 # type: ignore）。
        self._face_guard_menu_timer: Optional[QTimer] = None
        self._face_guard_menu_action: Optional[QAction] = None
        self._face_guard_arbiter = None
        self._face_guard_paused = False      # 看守超时后暂停，等用户点 Dock 恢复
        self._face_guard_next_retry = 0.0    # 没起来时下一次重试的时刻（单调钟）
        self._face_guard_logged_state = ""   # 只在状态真的变化时写日志，别半秒刷一条
        # v1.4.41 复核 F2：连续 FACE_GUARD_STALE_SEC 秒采不到画面 = 保护已中断（不是"运行中"）
        self._face_guard_stalled = False
        # v1.4.56 省电：屏幕锁定/显示器休眠时暂停采集（唤醒后自动继续看守）。
        self._face_guard_screen_locked = False
        self._face_guard_screen_probe_at = 0.0
        self._face_guard_last_fresh = 0.0    # 上一次"确实拿到新帧"的时刻（单调钟）
        self._face_guard_fed_ts = 0.0        # 上一次喂进迟滞的那份快照的时刻（去重用）
        # 诊断留痕：最近一次停守卫的原因与时刻。停会话的理由（缩小/关功能/中断/
        # 锁屏）此前只写在日志里，face_guard.state 归 off 之后远程就再也问不出
        # "它为什么停了"。
        self._face_guard_last_stop = {"reason": "", "at": 0.0}
        self._guard_timer = QTimer(self)
        # 这张表现在只是**看门狗**：判定主通路是采集线程数完新快照后 emit 的
        # `face_guard_wakeup`（排队信号）。留一条与采样同拍的兜底，是为了万一某次
        # emit 没送达（窗口正在销毁、事件循环被别的模态框占住）时，最迟一个节拍后
        # 仍然把该收的窗收掉 —— 保护不能依赖"信号一定到"。
        # 间隔与 FACE_GUARD_TICK_SEC 同源，改采样节拍不会把它甩在后面。
        # 实测（2026-10-12，真代码 + 15fps 喂帧，16 次）：收窗中位 0.57 秒（0.44~0.63），
        # 其中 0.38 秒是驻留本身；判据在 tests/test_face_guard.py 的
        # test_new_sample_is_judged_without_waiting_for_the_timer 与
        # test_same_snapshot_is_never_fed_twice —— 去掉信号通路或去掉去重，都会红。
        self._guard_timer.setInterval(int(sf_presence.FACE_GUARD_TICK_SEC * 1000))
        self._guard_timer.timeout.connect(self._on_face_guard_tick)
        # 用户此前就开过：等首帧与玻璃层就绪后再同步，不在构造期碰系统框架。
        QTimer.singleShot(2500, self._sync_face_guard)
        # 上一次安装若已安全回退，下次打开时只提示一次；不新增入口、不打断启动。
        QTimer.singleShot(0, self._show_previous_update_result)
        # 更新：启动15秒首检，之后6小时复查；失败用独立单发计时器退避重试。
        self._update_retry_timer = QTimer(self)
        self._update_retry_timer.setSingleShot(True)
        self._update_retry_timer.timeout.connect(self._check_updates_background)
        self._update_periodic_timer = QTimer(self)
        self._update_periodic_timer.setInterval(UPDATE_PERIODIC_MS)
        self._update_periodic_timer.timeout.connect(self._check_updates_background)
        self._update_maintenance = BackgroundMaintenance(UPDATE_PERIODIC_MS / 1000)
        if not native_cocoa_active() or not self._update_maintenance.start(self.maintenance_wakeup.emit):
            self._update_periodic_timer.start()
        QTimer.singleShot(UPDATE_INITIAL_DELAY_MS, self._check_updates_background)
        # v1.4.23 临时目录旧副本自愈：≤1.4.4 的残留副本被系统从 /tmp 重新拉起时，
        # 会在 PyQt6 导入期 SIGSEGV 并留下"程序意外退出"弹窗（2026-09-22 / 09-23 两份
        # v1.3.97 报告）。sf_bootstrap 守卫只在新产物里、管不到旧副本，只能由正式版
        # 把这些目录改名中性化。推迟到首帧/首轮行情/更新自检之后再跑，且全程后台线程。
        QTimer.singleShot(TEMP_SWEEP_DELAY_MS, self._sweep_temp_copies)
        QTimer.singleShot(STAGING_PRUNE_DELAY_MS, self._prune_update_staging)

    # ---------- 背景：毛玻璃 ----------
    def showEvent(self, e):
        """窗口首次显示后延迟注入毛玻璃：contentView 此时才就绪，addSubview 安全。
        冷启动优化：毛玻璃 NSVisualEffectView 创建约 40-55ms，若在 showEvent 内同步
        执行会阻塞首帧渲染与首个事件循环 tick，拖慢首条行情上屏；改为 singleShot(0)
        排到首帧渲染完成后注入，视觉上玻璃效果仅晚一个事件循环 tick（<50ms）出现。"""
        super().showEvent(e)
        from sf_startup_health import schedule as schedule_startup_health
        schedule_startup_health(self)
        if self._glass_pending:
            self._glass_pending = False
            QTimer.singleShot(0, self._inject_glass)
        # show / Dock 恢复 / Space 切换都必须要求一帧完整内容，不能复用价格局部脏区。
        self._partial_price = None
        self._chart_changed = True
        self.update()

    def _inject_glass(self):
        """首帧渲染后执行毛玻璃注入（延迟自 showEvent，避免阻塞首帧/首 poll）"""
        try:
            if self._setup_glass():
                self._bg_effect = True
                # 建立独立玻璃层后立即按主窗在前的顺序复位；避免 macOS 层级切换时
                # 背景层盖住 Qt 内容，留下可点击但没有文字/行情的空壳。
                self._force_top()
                # 玻璃层就绪才有东西可截；延后一会儿，别和启动首帧抢时间
                QTimer.singleShot(sf_dock_motion.PREWARM_DELAY_MS, self._dock_motion_prewarm)
            else:
                self._bg_effect = False
                log_line("glass effect unavailable, fallback translucent")
        except Exception as exc:
            self._bg_effect = False
            log_line("glass inject error, fallback translucent: %r" % (exc,))
        finally:
            self._partial_price = None
            self._chart_changed = True
            self.update()

    def moveEvent(self, e):
        super().moveEvent(e)
        self._sync_glass_frame()
        self._sync_open_tool_windows()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        # backing store 在拉伸、遮挡恢复和 Space 切换后可能不保留旧像素；下一帧必须
        # 完整重绘，禁止动画局部条带把头部、图表和底部一直留在空白状态。
        self._partial_price = None
        self._chart_changed = True
        self._sync_glass_frame()
        self.update()
        # 整窗帧已排队：序号供呼吸灯时钟判断本周期是否已被覆盖（见 _dot_tick）。
        # 必须放在 update() 之后，序号只表示「这一帧确实排上了」。
        self._resize_paint_seq += 1

    # ---------- 绘制 ----------

    # ---------- 交互 ----------
    def _height_collapsed(self):
        """v1.2.65：收起态高度改为调用 sf_ui_render.collapsed_height() 唯一实现。

        此前本方法与 _height_expanded 各写一份逐字相同的推导式（复制粘贴），且用
        COLLAPSED_ROW_H(=7，纯数字视觉高度) 充当含汉字行的字形高（实为
        BOTTOM_ROW_ASCENT=10），高度少算 3px → 价格区与底部行1 仅 6px（设计 8px）。
        """
        return collapsed_height()

    def _height_expanded(self):
        """5.4：展开态窗口高度 = 收起态高度（唯一实现） + 分时图高度。
        优先记忆上次展开高度（手动拉伸不丢失）。"""
        base_leave = collapsed_height()
        if self._expanded_h and self._expanded_h >= base_leave + CHART_MIN_H:
            return self._expanded_h
        return base_leave + CHART_H

    def _set_minute_interval(self, interval):
        """5.4：设置分时拉取间隔（收起 30s 省电省流；展开 1s 实时）
        3.3 v1.2.27：非法输入不再静默吞掉，记录 WARNING 日志便于排查（不影响原降级行为）。"""
        try:
            self._minute_interval = float(interval)
            # v1.4.10：分时间隔变了（展开 1s / 收起 30s），叫醒分时线程重算睡眠
            self._resched_loops()
        except (TypeError, ValueError) as exc:
            log_line("set_minute_interval: invalid interval %r: %r" % (interval, exc), "WARNING")

    def _toggle_chart(self):
        """折叠/展开分时图：改窗口总高度而非图区高度。

        收起时放宽分时拉取间隔作为兜底刷新，展开时恢复较短间隔；展开状态不跨启动保存。
        行情拉取不受影响。"""
        try:
            if not feature_enabled("toggle_chart"):
                return
            # v1.4.36：这是"收起之后卡片瞬移回拖动前的位置"的现场落点。第三方全屏
            # Space 里由系统接管的拖拽不会让 Qt 缓存的几何追上真值，而下面这一段
            # （min/max 同步钳制 + 收展动画的 resize）是手势之后的**第一次写几何**，
            # 它会把过时原点整个推回 AppKit。先把模型对齐到 WindowServer 再动高度。
            self._reconcile_model_with_server("toggle_chart")
            self._chart_expanded = not self._chart_expanded
            lowered_min = False  # v1.2.48(H1)：提前初始化，保证展开分支在 min 下探前
                                 # 抛异常时 except 不触达未定义变量（NameError 双保险）
            raised_max = False   # v1.2.65：同上，收起分支动画期间放开上限，异常时
                                 # 需把 max 锁回收起高度（否则系统缩放把手复活）
            # v1.4.64（全方位复核）：这里原先每次展开都往 plist 写一个
            # "chartExpanded" 键，而**全仓没有任何一处读回它** —— 展开态从来不会
            # 跨启动保留（DELIVERY 早已记着"会回到默认"）。只写不读等于留下一份
            # 没人消费的用户状态，还会让人以为它有持久化。真要保留展开态，
            # 应该在启动侧读回来，而不是把这行加回去。
            # v1.2.65：动画起点统一在**任何 min/max 变更前**捕获。Qt 的
            # setMinimumHeight/setMaximumHeight 会同步钳制几何高度，变更后读
            # self.height() 拿到的已是钳制后的值，动画起点必然失真。
            from_h = self.height()
            if self._chart_expanded:
                # 展开：目标高度 = 记忆展开高度（或默认 无图+CHART_H）
                # v1.2.35：动画期间必须放低下限；起点已在分支前捕获，
                # 下限仍要下探到 0，否则中间帧会被 min 钳住、过渡卡住不连续。
                # 待 _anim_tick 完成帧的 layout_minmax 再恢复 MIN_H/MAX_H。
                # v1.2.48(H1)：先算 target 再下探 min —— _height_expanded() 若抛异常，
                # min 尚未被改为 0，异常态不残留（配合下方 except 恢复双保险）。
                self._expanded_h = max(from_h, self._expanded_h)
                target = self._height_expanded()
                self.setMinimumHeight(0)
                lowered_min = True
                # v1.2.36：恢复系统级可缩放上限 —— 收起态曾把 max 锁到收起高度，
                # 这里必须放回 MAX_H，否则展开态下沿拉伸会被系统 max 挡住
                self.setMaximumHeight(MAX_H)
                self._set_minute_interval(CHART_EXPANDED_MINUTE_INTERVAL)
            else:
                # 收起：目标高度 = 无图高度
                # v1.2.65 第1轮走查修复（收起动画被吞）：原实现「先锁 min/max，
                # 后取 self.height() 作动画起点」是顺序错误 —— Qt 的 min/max 会
                # **同步**钳制几何高度，setMaximumHeight(target) 执行的瞬间
                # height() 就已被压到 target（展开态 260 → 收起态 145）。
                # 于是下方 begin_layout(self.height(), target) 拿到 from==to，
                # driver.tick 的守卫 layout_anim_to != layout_anim_from 不成立 →
                # 整段收起过渡动画被吞（高度瞬间跳变、无过渡），完成帧的
                # layout_minmax / glass_mask 两个收口动作也永不发出。
                # 与展开分支（v1.2.35/48）对称：先捕获起点，动画期间放开上限，
                # 由完成帧的 layout_minmax(False) 再锁死 —— v1.2.36/37 防
                # macOS 系统级缩放把手复活的语义保持不变，只是延后到完成帧。
                target = self._height_collapsed()
                self._expanded_h = max(from_h, self._expanded_h)
                self.setMinimumHeight(target)
                self.setMaximumHeight(MAX_H)
                raised_max = True
                self._set_minute_interval(CHART_COLLAPSED_MINUTE_INTERVAL)
            # v1.2.73：连续点击可能在上一轮展开尚未绘制首帧时立刻反向，
            # 此时当前高度本就等于新的目标高度（from_h == target）。若仍仅登记
            # 动画，driver 会视为"没有布局动画"，不会发出 layout_minmax 收尾；
            # 收起态的最大高度因此残留 MAX_H，表现为连续点后窗口能异常拉高。
            # 这不是需要播放的动画，立即按最终状态收口即可。
            if from_h == target:
                self._anim_driver.layout_anim_from = 0
                self._anim_driver.layout_anim_to = 0
                if self._chart_expanded:
                    self.setMinimumHeight(MIN_H)
                    self.setMaximumHeight(MAX_H)
                else:
                    self.setMinimumHeight(target)
                    self.setMaximumHeight(target)
                self.resize(self.width(), target)
                try:
                    self._sync_glass_frame(force_mask=True)
                except Exception as _swallow_exc:
                    log_swallow("stock_float_window._toggle_chart", _swallow_exc)
                self.update()
                return
            # 布局动画：180ms ease-out，复用 _anim_timer 驱动逐帧 resize + update
            # v1.2.48：动画状态登记下沉 AnimDriver.begin_layout
            # v1.2.65：起点用分支前统一捕获的 from_h（真实收展前高度）
            self._anim_driver.begin_layout(from_h, target, time.monotonic())
            self._anim_timer.start()
        except Exception as exc:
            log_line("toggle chart error: %r" % (exc,))
            # v1.2.48(H1)：展开分支异常时 min 已下探到 0 —— 恢复 MIN_H 兜底，
            # 避免窗口被压到低于 MIN_H 的异常几何残留。
            if self._chart_expanded and lowered_min:
                try:
                    self.setMinimumHeight(MIN_H)
                    self.setMaximumHeight(MAX_H)
                except Exception as _swallow_exc:
                    log_swallow("stock_float_window._toggle_chart", _swallow_exc)
            # v1.2.65：收起分支异常时 max 已放开到 MAX_H（动画期间需要）—— 必须
            # 重新锁回收起高度，否则异常态下 macOS 系统缩放把手复活，收起态被拉高。
            elif (not self._chart_expanded) and raised_max:
                try:
                    self.setMaximumHeight(self._height_collapsed())
                except Exception as _swallow_exc:
                    log_swallow("stock_float_window._toggle_chart", _swallow_exc)

    def _hit_btn(self, pos, extra=0):
        """顶部按钮命中检测：四个独立 28×32px 热区；松开可容忍少量手势漂移。"""
        w = self.width()
        candidates = []
        for order, (name, off) in enumerate(BTN_X.items()):
            cx = w - off
            dx, dy = abs(pos.x() - cx), abs(pos.y() - BTN_CY)
            if dx <= HIT_DX + extra and dy <= HIT_DY + extra:
                candidates.append((dx * dx + dy * dy, order, name))
        if candidates:
            # 热区刚好相接或松开容差重叠时，优先最近中心；正中等距则保持 BTN_X
            # 的视觉顺序归属，不能由按钮名字母顺序把 min 错投为 close。
            return min(candidates)[2]
        # 兼容旧版「目标价」标签热区：当前界面已隐藏标签，正常情况下不会命中。
        rect = getattr(self, "_target_label_rect", None)
        if rect is not None and not rect.isEmpty() and rect.contains(pos.x(), pos.y()):
            return "target"
        return None

    def _btn_at(self, pos):
        return self._hit_btn(pos)

    def _chart_hit(self, pos):
        """v1.2.26：分时图区域点击热区（x 扩展为整窗横向 [0, width]，收起态与展开态均含左右边距）。
        展开态命中分时图本体 [CHART_TOP, h-BOTTOM_SECTION_HIDDEN]；
        收起态命中股价行到窗口底的整条区域 [Y_PRICE, h-BOTTOM_BASE_HIDDEN]。
        点击股价或涨跌幅百分比同样可触发展开分时图。
        y 范围不变：顶部（y < 热区起点，含顶部按钮周围空白）仍非热区，
        短点击收展；超过系统拖动阈值后由 mouseMoveEvent 转为拖动窗口。"""
        if not (0 <= pos.x() <= self.width()):
            return False
        if self._chart_expanded:
            return CHART_TOP <= pos.y() <= self.height() - BOTTOM_SECTION_HIDDEN
        return Y_PRICE <= pos.y() <= self.height() - BOTTOM_BASE_HIDDEN

    def _trigger_top_button(self, btn):
        """执行顶部按钮动作。缩小/关闭排队执行，避免在鼠标事件栈中改变主窗状态。"""
        # 点击就是用户的明确动作：还没弹出的气泡不要在面板打开后再冒出来。
        pending_show = getattr(self, "_bubble_show_timer", None)
        if pending_show is not None:
            pending_show.stop()
        try:
            if btn == "close":
                QTimer.singleShot(0, self.quit)
            elif btn == "min":
                QTimer.singleShot(0, self.minimize_to_dock)
            elif btn == "edit":
                self.open_editor()
            elif btn in ("bell", "target"):
                self._open_target_panel()
            log_line("btn action queued: %s" % btn, "INFO")
            return True
        except Exception as exc:
            log_line("btn action error (%s): %r" % (btn, exc), "WARNING")
            return False

    def _stretch_floor_height(self):
        """下沿拉伸的**语义**地板：展开态 MIN_H，收起态收起高度。

        为什么不用 `self.minimumHeight()`（v1.4.34 改的）：收展动画期间那个值是被
        **主动**降到 0 的 —— 见 `_toggle_chart` 展开分支里 v1.2.35 那段注释（不降到 0，
        动画中间帧会被 MIN_H 钳住、收展过渡不连续）。把一个"动画期间临时为 0"的值
        当限高公式的地板，手势只要落进那 200 毫秒，就能把主窗一路拖到 height=0
        （离屏合成事件实测复现；静止展开态同一个手势最低是 224，所以用户看到的是
        "偶尔"）。用户能接受多矮是语义问题，不该跟着动画实现抖，所以这里只认常量。
        """
        return MIN_H if self._chart_expanded else self._height_collapsed()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.RightButton:
            sf_menu_trace.note_right_press()    # 与随后的菜单打开次数对账
        if e.button() == Qt.MouseButton.LeftButton:
            # v1.4.18 走查（S5）：一次新按下 = 一段新手势。上方各分支只置自己那一个
            # 标志，从不清另外两个。而代码自陈 macOS 原生置顶/跨 Space 偶发丢
            # mouseRelease —— 一旦拖拽/拉伸的松开事件丢失，_drag/_resizing/_press_chart
            # 会残留为 True；下一次在中部按下（_drag=True）时 _resizing 仍为真，
            # mouseMoveEvent 的 `if self._resizing and 左键` 就会把"移动窗口"误当成
            # "拉伸高度"。在按下入口统一复位，杜绝这类粘滞手势。
            self._drag = False
            self._resizing = False
            self._press_chart = False
            self._chart_press_global = None
            self._sys_drag = False
            btn = self._btn_at(e.pos())
            if btn:
                self._press_btn = btn
                self._press_btn_actioned = False
                self._drag = False
                self.update()  # 立即绘制按下态，反馈本次点击已被接收
                # 缩小与关闭属于不带草稿的直接动作。macOS 上原生置顶/跨 Space
                # 时偶发收不到 mouseRelease，若仍等松开才执行，就会表现为“点了没反应”。
                # 这两个入口改在确认按下命中后立即执行；其余会打开/关闭编辑内容的入口
                # 仍保留松开触发，避免误触造成内容面板跳出。
                if btn in ("min", "close"):
                    self._press_btn_actioned = self._trigger_top_button(btn)
            elif self._chart_hit(e.pos()):
                # Preserve click-to-toggle until the pointer crosses the drag threshold.
                self._press_chart = True
                self._drag = False
                self._press_btn = None
                self._chart_press_global = e.globalPosition().toPoint()
                self._offset = self._chart_press_global - self.frameGeometry().topLeft()
            elif self._chart_expanded and e.pos().y() >= self.height() - 8:
                # 底部边缘（仅展开态）：进入调整高度模式；
                # v1.2.28：收起态下该区域不响应拉伸，落回普通拖拽/点击
                self._resizing = True
                self._press_btn = None
                self._resize_start = e.globalPosition().toPoint()
                self._resize_h0 = self.height()
                self._resize_hold_until = time.monotonic() + RESIZE_HOLD_SEC
            else:
                self._drag = True
                self._press_btn = None
                self._offset = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
                # v1.4.29（开关 system_drag，默认关）：把位移交给 macOS 系统。
                # 自绘拖拽的问题不在"每拍 0.28~0.53ms 的处理耗时"，而在**尾巴**：
                # 一次 4.2ms 整窗绘制或一次偶发 6ms 置顶复核插进两拍之间，窗口就
                # 在这一帧里不动。交给 WindowServer 后位移与主线程解耦。
                # startSystemMove() 只在"当前正在处理一个真实指针事件"时返回 True
                # （合成事件下恒 False，实测），所以这里必须处理返回值：False 就
                # 原样走下面的自绘拖拽，不能既不起系统拖拽又不自己移动窗口。
                if feature_enabled("system_drag"):
                    try:
                        wh = self.windowHandle()
                        if wh is not None and wh.startSystemMove():
                            self._sys_drag = True
                            self._drag = False
                            # 系统拖拽会让 AppKit 进入跟踪模式模态循环，Qt 定时器在那
                            # 段时间不被唤醒 → 背景层必须改由原生 run loop 盯着挪，
                            # 否则实测掉队 44px。见 sf_glass._glass_follow_begin。
                            self._glass_follow_begin()
                    except Exception as exc:
                        log_swallow("stock_float_window.mousePressEvent.system_drag", exc)

    def mouseMoveEvent(self, e):
        if self._press_chart and e.buttons() & Qt.MouseButton.LeftButton:
            origin = getattr(self, "_chart_press_global", None)
            if origin is not None:
                if (e.globalPosition().toPoint() - origin).manhattanLength() < QApplication.startDragDistance():
                    return
                self._press_chart = False
                self._drag = True
        if self._resizing and e.buttons() & Qt.MouseButton.LeftButton:
            dh = e.globalPosition().toPoint().y() - self._resize_start.y()
            # v1.4.34：地板取**语义高度**（见 _stretch_floor_height 的 why），
            # 不再取那个收展动画期间临时为 0 的 Qt 下限。
            new_h = max(self._stretch_floor_height(), min(MAX_H, self._resize_h0 + dh))
            # v1.2.28：拉伸帧合并 —— 仅记录目标高度，由 16ms 动画定时器单帧应用一次
            # resize，避免每次 mouseMove 同步 resize+全量重绘+玻璃 mask 重建的
            # resize 风暴导致帧率不稳、上方（顶部圆角/名称区）卡顿与跳动。
            if getattr(self, "_resize_target_h", -1) != new_h:
                self._resize_target_h = new_h
                # 逐拍续期：只要鼠标还在动，合并时钟就不停表（见 _anim_tick_inner）
                self._resize_hold_until = time.monotonic() + RESIZE_HOLD_SEC
                if not self._anim_timer.isActive():
                    self._anim_timer.start()
        elif self._drag and e.buttons() & Qt.MouseButton.LeftButton:
            self.move(e.globalPosition().toPoint() - self._offset)
        elif getattr(self, "_sys_drag", False) and e.buttons() & Qt.MouseButton.LeftButton:
            # 系统拖拽进行中：位移不归本进程管，光标/悬停反馈也不要在中途改
            # （松手后 moveEvent/下一次悬停自然会纠正）。
            # **必须同时判左键还按着**：macOS 在置顶/跨 Space 时偶发不投
            # mouseRelease（见 mousePressEvent 的粘滞手势注释），只看 `_sys_drag`
            # 的话，丢一次松开就会让此后每个移动事件都被这条 return 吃掉 ——
            # 悬停高亮和底部拉伸光标永久失灵，直到用户再点一下。旧写法
            # `if self._drag and buttons & LeftButton` 天然会自己退回去，这里保持同等健壮性。
            return
        else:
            # 鼠标离开已按下按钮的热区时立即取消按下态；若系统因置顶/跨 Space
            # 未投递 mouseRelease，不能把缩小按钮永久画成悬浮/按下状态。
            if self._press_btn is not None and self._btn_at(e.pos()) != self._press_btn:
                self._press_btn = None
                self._press_btn_actioned = False
                self.update()
            # 光标反馈：底部边缘（仅展开态）显示上下调整光标
            if self._chart_expanded and e.pos().y() >= self.height() - 8:
                self.setCursor(Qt.CursorShape.SizeVerCursor)
            else:
                self.unsetCursor()
            btn = self._btn_at(e.pos())
            if btn != self._hover_btn:
                self._hover_btn = btn
                self._update_target_tooltip(btn, e.globalPosition().toPoint())
                # v1.4.28：悬停换态改走统一入口 `_repaint_now()`。规则与
                # `_repaint_now` 的既有约定一致 —— 主画钟（16ms 呼吸灯）在跑时由它
                # 兜帧，不再另外排一帧整窗重绘（两个同频来源落在不同事件循环轮次，
                # Qt 合并不了，是本项目记录过的"双倍白画"坑）。主画钟没跑（收起图表 /
                # 盘后 / 无数据）时它退化成 `update()`，行为与改前一致。
                # 如实记录：本轮改后复测**没有**量到绘制次数下降（86 → 85 次/场景），
                # 因为悬停扫过的大多数点并不命中按钮，改前也极少真的排到帧。保留这条
                # 改动是因为它消除的是已知的真实坑，且代价为零。
                self._repaint_now()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            # 松开允许 6px 手势漂移，但仍必须归属按下的同一独立按钮。
            btn = self._hit_btn(e.pos(), PRESS_RELEASE_SLOP)
            # v1.3.58：btn 用扩大后的热区只用于“仍是同一按钮”的确认；按下时
            # 记录的按钮才是唯一动作归属。这样边界/容差区不会把一次铃铛点击
            # 错投到缩小按钮，同时允许触控板的 6px 正常释放漂移。
            if self._press_btn and self._press_btn == btn:
                # 缩小/关闭已在按下时执行，松开只负责清理视觉状态，防止重复执行。
                if not getattr(self, "_press_btn_actioned", False):
                    # v1.2.87：互斥禁用入口（图标已置灰）忽略点击，避免误操作
                    disabled = False
                    try:
                        disabled = (btn in ("edit", "bell")) and self._is_btn_disabled(btn)
                    except Exception as exc:
                        # 保持 fail-open（按可用处理），但必须记日志 —— 否则互斥
                        # 静默失效、完全无从排查。不改 fail-close：判定异常时置灰
                        # 会让按钮永久不可用，比放过一次误操作更糟。
                        log_line("btn disabled check error: %r" % (exc,), "WARNING")
                        disabled = False
                    if disabled:
                        self._show_btn_blocked_feedback(btn)
                    else:
                        self._trigger_top_button(btn)
            # v1.2.6：点击分时图热区切换展开/收起（按下与松开均在热区内才触发，避免拖拽误触）
            chart_origin = getattr(self, "_chart_press_global", None)
            chart_stationary = (chart_origin is None or
                                (e.globalPosition().toPoint() - chart_origin).manhattanLength()
                                < QApplication.startDragDistance())
            if self._press_chart and chart_stationary and self._chart_hit(e.pos()):
                self._toggle_chart()
            self._press_chart = False
            self._chart_press_global = None
            # 调整高度结束后保存（持久化）
            if self._resizing:
                # v1.2.28：松开瞬间若定时器尚未应用最后一次目标高度，就地补齐一次
                # （避免保存/记忆的高度落后于鼠标最终位置）
                try:
                    th = getattr(self, "_resize_target_h", None)
                    if th is not None and abs(self.height() - th) > 1:
                        self.resize(self.width(), th)
                except Exception as _swallow_exc:
                    log_swallow("stock_float_window.mouseReleaseEvent", _swallow_exc)
                self._resize_target_h = None
                self._after_save_config(save_config(self.code, self.x(), self.y(), self.height(), self._bg_cfg))
                # v1.2.20：拉伸结束强制重建一次玻璃层圆角 mask，节流期间的尺寸差在此对齐
                try:
                    self._sync_glass_frame(force_mask=True)
                except Exception as _swallow_exc:
                    log_swallow("stock_float_window.mouseReleaseEvent", _swallow_exc)
                self._resizing = False
                # 5.4 v1.2.5：手动拉伸后记忆展开高度（展开态下）
                if self._chart_expanded and self.height() > self._height_collapsed():
                    self._expanded_h = self.height()
                # v1.2.28：拉伸结束，若无进行中的布局/价格动画，停止拉伸合并定时器
                try:
                    if (getattr(self, "_layout_anim_to", 0) == getattr(self, "_layout_anim_from", 0) and
                            getattr(self, "_anim_to_price", 0) == getattr(self, "_disp_price", 0) and
                            getattr(self, "_anim_to_pct", 0) == getattr(self, "_disp_pct", 0) and
                            not self._has_active_roll_animation()):
                        self._anim_timer.stop()
                        # v1.2.52：停表同时清除滚动目标残留（防泄漏态下停表把
                        # 渲染层永久锁在旧目标串）
                        self._roll_pending = {}
                except Exception as _swallow_exc:
                    log_swallow("stock_float_window.mouseReleaseEvent", _swallow_exc)
            # 拖拽结束后：边缘吸附 + 保存位置与高度（持久化）
            # `_sys_drag`：系统拖拽同一条收口。万一 macOS 没投这个松开事件（置顶/跨
            # Space 时实测会发生），最坏是这次不吸附、位置由 `geometryChanged` 的
            # `_clamp_to_screen(force=True)` 落盘兜住；下一次按下会清掉该标志。
            if self._drag or getattr(self, "_sys_drag", False):
                was_sys = bool(getattr(self, "_sys_drag", False))
                if was_sys:
                    # 停掉原生跟随定时器，并就地把背景层对齐一次：
                    # 拖拽期间的任何残余误差在松手这一帧必须归零。
                    try:
                        self._glass_follow_end()
                    except Exception as _swallow_exc:
                        log_swallow("stock_float_window.mouseReleaseEvent.follow_end",
                                    _swallow_exc)
                    # v1.4.36：先把 Qt 缓存的原点对齐到 WindowServer 真值，再吸附。
                    # 顺序不能反：`_snap_to_edge` 读的就是这个缓存 —— 缓存还停在
                    # 拖动前的位置时，它算出来的"贴边落点"是照着旧位置算的，
                    # 一次 move() 就把卡片搬回半路（与 `_toggle_chart` 同一根因）。
                    try:
                        self._reconcile_model_with_server("drag_end")
                    except Exception as _swallow_exc:
                        log_swallow("stock_float_window.mouseReleaseEvent.resync",
                                    _swallow_exc)
                self._snap_to_edge()
                if was_sys:
                    # 松手这一帧必须把背景层对齐到**内容窗的真实位置**，而不是对齐到
                    # AppKit 的模型：系统拖拽期间模型可以落后几百像素，而"松手后模型
                    # 会自己追上"只是通常成立，不是保证 —— 一旦它没追上（第三方全屏
                    # Space 切换就是嫌疑场景），照模型摆会把跟随定时器已经抢对的
                    # 位置又搬回半路，且 `_top_state_ok` 从此看不见（两模型一致地错）。
                    try:
                        self._sync_glass_frame(force_mask=True, use_server_origin=True)
                    except Exception as _swallow_exc:
                        log_swallow("stock_float_window.mouseReleaseEvent.glass_sync",
                                    _swallow_exc)
                self._after_save_config(save_config(self.code, self.x(), self.y(), self.height(), self._bg_cfg))
            self._press_btn = None
            self._drag = False
            self._sys_drag = False
            self.update()  # 清除按下态
            if btn != self._hover_btn:
                self._hover_btn = btn
                self._update_target_tooltip(btn, e.globalPosition().toPoint())
                self.update()

    def _target_bubble_rows(self):
        """铃铛气泡的行数据：(待触发行, 已触发行)，价位按本品种小数位，百分比取当前判定价。"""
        return bell_bubble_rows(self._safe_alerts(), self._current_price(),
                                price_decimals(self.code))

    def _stop_bubble_timers(self):
        """取消「待弹出」与「待收起」两张单次定时器（没建过就跳过）。"""
        for name in ("_bubble_show_timer", "_bubble_hide_timer"):
            timer = getattr(self, name, None)
            if timer is not None:
                timer.stop()

    def _hide_target_bubble(self):
        """立刻收起气泡并取消所有待办的弹出/收起（离开整窗、缩进 Dock 走这里，不留缓冲）。"""
        self._stop_bubble_timers()
        bubble = getattr(self, "_target_bubble", None)
        if bubble is not None:
            bubble.hide()

    def _place_target_bubble(self, bubble):
        # 锚点 = 铃铛图标底边中心：BTN_X 是距窗口右边缘的偏移，需换算成窗口内 x。
        # 同时给出图标上沿，下方放不下时气泡整块翻到铃铛上方。
        anchor = QPoint(self.width() - BTN_X["bell"], BTN_ICON_Y + BTN_ICON_SZ)
        top = QPoint(anchor.x(), BTN_ICON_Y)
        bubble.show_below(self.mapToGlobal(anchor), self.mapToGlobal(top).y())

    def _update_target_tooltip(self, btn, global_pos):
        """悬停按钮变了：安排铃铛气泡的弹出或收起（不在这里直接弹）。

        节拍（常量与来源见 sf_constants.BELL_BUBBLE_*）：
        · 进入铃铛：气泡没开就等停满 DWELL 再弹，划过不弹；已经开着就取消待收起并刷新内容；
        · 离开铃铛：气泡开着就留 HIDE_GRACE 再收，期间滑回来则取消；没开就取消待弹出。
        定时器没建（测试替身宿主）时退回立即执行，行为与改前一致。"""
        bubble = getattr(self, "_target_bubble", None)
        if bubble is None:
            return
        show_timer = getattr(self, "_bubble_show_timer", None)
        hide_timer = getattr(self, "_bubble_hide_timer", None)
        if btn == "bell":
            if hide_timer is not None:
                hide_timer.stop()
            if bubble.isVisible() or show_timer is None:
                self._show_target_bubble_now()
            else:
                show_timer.start(BELL_BUBBLE_DWELL_MS)
            return
        if show_timer is not None:
            show_timer.stop()
        if bubble.isVisible() and hide_timer is not None:
            hide_timer.start(BELL_BUBBLE_HIDE_GRACE_MS)
        else:
            bubble.hide()

    def _show_target_bubble_now(self):
        """按当前目标价立即摆放并显示气泡；没有任何目标就收起。"""
        bubble = getattr(self, "_target_bubble", None)
        if bubble is None:
            return
        pending, hit = self._target_bubble_rows()
        if pending or hit:
            bubble.setRows(pending, hit)
            self._place_target_bubble(bubble)
        else:
            bubble.hide()

    def _on_bubble_show_due(self):
        # 定时器到点时再核对一次：期间鼠标若已离开铃铛，就不弹。
        if self._hover_btn == "bell":
            self._show_target_bubble_now()

    def _on_bubble_hide_due(self):
        if self._hover_btn != "bell":
            bubble = getattr(self, "_target_bubble", None)
            if bubble is not None:
                bubble.hide()

    def _refresh_target_bubble(self):
        """气泡正开着时跟着行情更新：百分比随现价走，触发后行从待触发挪到已触发。

        只在气泡可见且鼠标仍在铃铛上时才做事；内容没变不重排、不重新前置，
        所以盘中每个 tick 过来的开销只是一次行数据比较。
        """
        bubble = getattr(self, "_target_bubble", None)
        if bubble is None or not bubble.isVisible() or self._hover_btn != "bell":
            return
        pending, hit = self._target_bubble_rows()
        if not (pending or hit):
            bubble.hide()
        elif bubble.setRows(pending, hit):
            self._place_target_bubble(bubble)

    def enterEvent(self, e):
        super().enterEvent(e)

    def leaveEvent(self, e):
        # 鼠标离开整个主窗时，macOS 不保证再投递 mouseMove/mouseRelease。
        # 必须同时清掉悬停和按下状态并立即重绘，否则缩小图标会残留灰色底板。
        changed = self._hover_btn is not None or self._press_btn is not None
        self._hover_btn = None
        self._press_btn = None
        self._press_btn_actioned = False
        self._hide_target_bubble()
        if changed:
            self.update()
        super().leaveEvent(e)

    def contextMenuEvent(self, e):
        from PyQt6.QtWidgets import QMenu
        # v1.2.65 第1轮走查：连续右键（上一次菜单尚未关闭就再次右键）会覆盖
        # self._ctx_menu，被覆盖的那个从此无人引用、也无人销毁 —— 先就地销毁。
        self._on_ctx_menu_closed()
        m = QMenu(self)
        m.setStyleSheet("""
QMenu {
    background-color: rgba(38, 43, 58, 0.97);
    border: none;
    border-radius: 14px;
    padding: 6px;
    color: @FG_BRIGHT@;
    font-size: 13px;
}
QMenu::item {
    padding: 7px 26px 7px 12px;
    border-radius: 6px;
    background: transparent;
    color: @FG_BRIGHT@;
}
QMenu::item:selected {
    background-color: @ACCENT@;
    color: @FG_BRIGHT@;
}
/* v1.4.54：勾号挪到贴近文字的位置。离屏实测（同一渲染路径，像素级量取）：
   默认勾画在 x10..16、文字从 x34 起，中间净空 18px；relative 左移 14px 后
   勾在 x24..30、文字位置不动，净空剩 4px。只动 :checked 的绘制位置、
   不动占位（unchecked 本来就什么都不画），所以布局与未勾时完全一致。 */
QMenu::indicator:checked {
    position: relative;
    left: 14px;
}
/* v1.4.71：行首图标离行首留呼吸空间（负责人看图指出"贴着左边缘"）。
   不写这一条时图标画在 x8..24，而悬停的高亮胶囊从 x6 起 —— 左边只剩 2px，
   那枚图几乎压在蓝色圆角边上。
   ⚠ 别改 `QMenu::item { padding-left }` 去挪它：本机离屏实测那个值**只挪文字**
   （12→18 时文字 43→49，图标纹丝不动停在 x8），图标列的位置由 Qt 自己定。
   `QMenu::icon { left }` 才是这一格的偏移，且不影响文字与整宽：
   8px 之后图标 16..32、文字仍是 43 —— 左边距 10 与"图标到文字"的 11 基本对称。 */
QMenu::icon {
    left: 8px;
}
QMenu::item:disabled {
    color: @FG_DIM@;
}
QMenu::separator {
    height: 1px;
    background: @MENU_SEP@;
    margin: 5px 8px;
}
""".replace("@FG_DIM@", FG_DIM_HEX)
        .replace("@FG_BRIGHT@", FG_BRIGHT_HEX)
        .replace("@ACCENT@", ACCENT_BLUE_HEX)
        .replace("@MENU_SEP@", MENU_SEPARATOR_HEX))
        # v1.4.60（复核 F6）：禁用项的颜色原先手写 #6b7280 —— 那正是本轮判定不达 AA
        # 而被提亮掉的旧值，菜单里"点不动的那几行"因此还是难读。现在样式表里不许
        # 再出现裸 hex，次要文字灰只有一个来源 sf_constants.FG_DIM_HEX。
        # v1.2.35：右键菜单精简 —— 移除「替换股票」「最小化到 Dock」「退出」菜单项
        # （底层功能保留：编辑仍走标题栏编辑按钮、退出仍走关闭按钮/Dock，仅 UI 层不可见）
        # v1.4.54：菜单顺序按用户看图指定的来 —— 刷新行情、自动隐藏窗口、
        # 检查更新、导出诊断包、版本号。分隔线只留版本行前那一条。
        # 历史菜单配置见下：菜单行图标由各行实际需要决定，版本行不加（它是禁用项）。
        # ⚠ 一条 Qt 机制记在这里，别当 bug 查：**同一行有图标时 Qt 不画勾选框**。
        #   本机离屏实测：无图标 + checked 的勾画在 x12..23，给同一行加图标后那一列
        #   整列空白、文字位置不动 —— 图标与勾号抢的是同一格。所以「自动隐藏窗口」的
        #   开/关改由图标本身表达（睁眼 / 划掉的眼），取值仍是 v1.4.67 定下的那一个
        #   口径 `_face_guard_switch_on()`，且与勾号挂在同一个 300ms 刷新回调上 ——
        #   "显示"与"点下去会切成什么"不许各说各话。
        #   取不到图（无 QtSvg）时不放图标，那一行自动退回"无图标 + 勾号"的老形状。
        _set_menu_icon(m.addAction("刷新行情", self._force_fetch), "refresh", tag="refresh")
        # ---- v1.4.40 人脸在场守卫 ----
        # 用 triggered 而非 toggled：toggled 在 setChecked（初始化菜单）时也会发，
        # 于是"每次打开菜单"都会被当成一次用户操作，反过来又写一遍配置。
        # 目标状态取当前显示口径的反值，不读 act.isChecked() —— 后者取决于
        # Qt 先发 triggered 还是先翻勾选，顺序一变就是"菜单动了但功能没动"。
        if feature_enabled("face_guard"):
            _g = m.addAction(FACE_GUARD_MENU_TEXT)
            if _g is not None:
                _g.setCheckable(True)
                _g.setData("face_guard")
                # 勾的口径与点击目标共用 `_face_guard_switch_on()`（v1.4.67）：
                # running 算开，starting（刚点下、会话正在起的那 0.2~0.6 秒）也算开
                # —— 否则勾号每 300ms 被自己的刷新按回去一次，用户看到"第一次点击
                # 没反应，第二次才生效"。needs_auth/denied 等起不来的状态一律不勾
                # （不许说好话）；完整状态在诊断包 face_guard.json 的 status_text。
                # v1.4.51 起这一项**下面不再有灰字状态行**（用户要求菜单不展示提示语）；
                # v1.4.52 起首次开启的那次说明框也撤了（用户看图要求「永不出现」）。
                _on = self._face_guard_switch_on()
                _g.setChecked(_on)
                _set_menu_icon(_g, "eye_open" if _on else "eye_closed")
                _g.triggered.connect(
                    lambda _=False: self._on_face_guard_toggled(
                        not self._face_guard_switch_on(), _g))
                # v1.4.54：菜单开着的时候勾号必须**跟真实状态走**，不能只在每次弹出
                # 时算一遍 —— 菜单开着时状态翻转（比如守卫自己收窗、起会话失败）
                # 也完全看不见。300ms 刷新一次，值永远取自同一个口径
                # `_face_guard_switch_on()`（勾号与点击目标不许各说各话）。
                self._face_guard_menu_action = _g
                self._face_guard_menu_timer = QTimer(self)
                self._face_guard_menu_timer.setInterval(300)
                self._face_guard_menu_timer.timeout.connect(
                    self._refresh_face_guard_menu_check)
                self._face_guard_menu_timer.start()
        _set_menu_icon(m.addAction("检查更新",
                             lambda: self._check_updates_background(manual=True)), "update", tag="update")
        # v1.4.51：这里**不再放**那行灰字（"自动检查：启动后 15 秒，之后每 6 小时…"）。
        # 用户 2026-10-09 看图明确要求"右键菜单中的提示语去掉，不展示"。
        # 节奏数字仍然只有 `sf_constants` 一个来源；要查"实际多久查一次"看
        # 诊断包 `runtime.json` 与日志里的 `update check done` 行。
        # 目标价功能仅保留右上角铃铛入口；右键菜单不再重复展示该入口。
        # v1.4.69：文案由「导出诊断信息」改为「导出诊断包」（用户看图要求）——
        # 与它落到桌面上的文件名 `Skyrocket-诊断-<时间戳>.zip`、以及各处对外材料
        # 说的"诊断包"对齐，不再一个叫法两个名字。
        _set_menu_icon(m.addAction("导出诊断包",
                                  lambda: self._export_diagnostics(background=True)), "diag", tag="diagnostics")
        # The menu must finish closing before feedback capture creates another
        # top-level window. Creating it inside QMenu's mouse handler can lose
        # the first click while the native menu backend is being torn down.
        _set_menu_icon(m.addAction("反馈问题", self._queue_feedback_open), "diag", tag="feedback")
        m.addSeparator()
        # 版本号：灰色禁用项（不可点击），从打包后 .app 的 Info.plist 读取，
        # 开发态（无 Info.plist）回退 sf_constants.APP_VERSION。
        # 发版标签（单 feed 可见标识）：Beta 版在版本号后加 " Beta"，稳定版裸版本号。
        # 后缀字串走 sf_constants.BETA_SUFFIX（2026-10-12 收口）：这条规则此前在这里和
        # VERSION_LABEL 各写一份，转正式时漏改一处，菜单就会继续说 Beta。
        _v = m.addAction("v" + _bundle_version()      # v1.4.53：去掉「版本」两个字（用户看图要求）
                         + (BETA_SUFFIX if RELEASE_STAGE == "beta" else ""))
        _v.setEnabled(False)
        _v.setData("version")
        # 2026-10-23（用户看图选定）：版本号改用股价同款数字字体 D-DIN，常规 14px、字距 +0.6。
        # 只设这一行；其余四行与整个菜单的样式表字体不动。"Beta" 与版本号同属一个字符串，
        # 会一并换成 D-DIN（要保持原字体需把这一行做成自定义控件，本轮不做）。
        # D-DIN 没解析到时 _menu_version_font() 返回 None，这一行保持原字体，不降级成半吊子。
        _vf = _menu_version_font()
        if _vf is not None:
            _v.setFont(_vf)
        # 注意：macOS 上 QMenu.exec() 会走原生 NSMenu 渲染，QSS 样式(圆角/背景/描边)全部失效，
        # 因此改用非阻塞 popup() 让 Qt 自绘样式生效；同时持有引用防止菜单被 GC 关闭。
        self._ctx_menu = m
        sf_menu_trace.begin_menu(m, context=lambda: _menu_context(self))
        m.aboutToHide.connect(lambda menu=m: self._on_ctx_menu_closed(menu))
        m.popup(e.globalPos())  # PyQt6 适配：QContextMenuEvent 无 globalPosition()，用 globalPos()

    def _queue_feedback_open(self):
        """Open feedback after the context menu has completed its close event."""
        if getattr(self, "_feedback_open_pending", False):
            sf_menu_trace.feedback_request(dropped=True)     # 上一次还没处理完：这次被丢掉，记一笔
            return
        self._feedback_open_pending = True
        sf_menu_trace.feedback_request(sf_menu_trace.open_session_id())
        QTimer.singleShot(0, self._open_feedback)

    def _open_feedback(self):
        self._feedback_open_pending = False
        attempt = sf_menu_trace.feedback_begin()
        opened_at = time.monotonic()
        from sf_feedback_capture import capture_snapshot
        from sf_feedback_ui import FeedbackDialog
        from sf_constants import APP_SUPPORT_DIR, FEEDBACK_ENDPOINT
        from sf_logging import RUN_ID
        dialog = getattr(self, "_feedback_dialog", None)
        if dialog is not None and dialog.stage == "received" and not dialog.isVisible():
            dialog.deleteLater()
            self._feedback_dialog = None
            dialog = None
        reused = dialog is not None
        if dialog is None:
            try:
                snapshot = capture_snapshot(self, app_version=_bundle_version(),
                                            bundle_id=BUNDLE_ID, run_id=RUN_ID, index_only=True)
                sf_menu_trace.feedback_mark(attempt, "captured_ms")
                dialog = FeedbackDialog(
                    self, snapshot=snapshot,
                    storage_dir=os.path.join(APP_SUPPORT_DIR, "feedback"), endpoint=FEEDBACK_ENDPOINT,
                    diagnostic_capture=lambda context: self._export_diagnostics(
                        prepare_only=True, feedback_context=context),
                    capture_again=lambda: capture_snapshot(
                        self, app_version=_bundle_version(), bundle_id=BUNDLE_ID, run_id=RUN_ID, index_only=True))
                sf_menu_trace.feedback_mark(attempt, "built_ms")
                self._feedback_dialog = dialog
            except Exception as exc:
                log_line("feedback capture failed: %s" % type(exc).__name__, "ERROR")
                QToolTip.showText(self.mapToGlobal(QPoint(self.width() // 2, 10)),
                                  "诊断采集失败，未上传。请使用“导出诊断包”。", self)
                sf_menu_trace.feedback_finish(attempt, "capture_failed", error=type(exc).__name__)
                return
        # 居中在屏幕上而不是父窗口上（Qt 默认居中父窗口，而父窗口是屏幕一角的小卡片）。
        dialog.present()
        sf_menu_trace.feedback_mark(attempt, "presented_ms")
        # 与目标价面板、编辑器同一条抬层：主窗层级是 25，反馈窗默认只有 8，会被主窗压在下面。
        # 测试替身宿主没有这个方法，取不到就跳过。
        raise_tool = getattr(self, "_raise_tool_window", None)
        if raise_tool is not None:
            raise_tool(dialog)
        sf_menu_trace.feedback_mark(attempt, "raised_ms")
        # 留一行耗时：首次打开要现建窗口，用户若反映「点了没反应」，这里是唯一的现场证据。
        log_line("feedback dialog opened in %dms visible=%s"
                 % ((time.monotonic() - opened_at) * 1000, dialog.isVisible()))
        sf_menu_trace.feedback_finish(attempt, "opened", dialog, reused=reused)

    def _show_previous_update_result(self):
        """把安装助手留下的最终结果翻译为一次性系统提示。"""
        try:
            import sf_update
            result = sf_update.consume_install_result()
        except Exception as exc:
            log_line("update result read failed: %r" % (exc,), "WARNING")
            return
        if not result or result["status"] == "installed":
            return
        if result["status"] == "rolled_back":
            self._show_update_message(
                "更新未完成", "更新未完成，已自动保留原版本。",
                "当前版本可以继续正常使用。")
        elif result["status"] == "rollback_failed":
            self._show_update_message(
                "更新需要处理", "更新未完成，且自动恢复原版本失败。",
                "请在“应用程序”中确认「Skyrocket」是否仍可打开。")
        else:
            self._show_update_message(
                "更新未完成", "更新未完成，当前版本未受影响。")

    def _prune_update_staging(self):
        """把 updates/ 里没人认领的下载残留挪走（一律 mv，一个文件都不删）。

        起因很实在：诊断包第一次真跑就读出本机这里有 **80,382,786 字节**，含一份
        26MB 的 `.part-*` 半包 —— v1.4.59 的剪枝只做在「应用程序」里的回退包上，
        staging 这一侧从来没人扫过，而每次更新失败/中断都会再攒一份。

        三条不许放宽的边界：
          · 只在冻结产物里跑（与临时清扫同口径）：源码态与测试跑的是**真用户目录**
            下的 updates/，挪走别人正在用的东西不是自愈；
          · 一小时内改过的一个都不碰（`sf_update.residue_reason` 的 grace 判据），
            正在下载的半包被挪走 = 这一次更新白跑；
          · 只 mv 到系统临时目录专用文件夹，不删文件；`update-result.json` 与
            助手脚本副本**不在清理名单里**（前者是用户下次启动该看到的那一次提示，
            后者可能正被 /bin/sh 逐行读）。
        结果一行日志 + 留在 `sf_update.staging_prune_report()` 里进诊断包
        （AGENTS §5.2 那条口径：只在日志里说不算告知，日志会被轮转挤掉）。
        """
        if getattr(self, "_threads_stopped", False):
            return
        if not getattr(sys, "frozen", False):
            return

        def worker():
            try:
                import sf_update
                report = sf_update.prune_staging_residue()
                if report["moved"]:
                    log_line("update staging 清理: 挪走 %d 份 / %d 字节 → 系统临时目录"
                             % (len(report["moved"]), report["moved_bytes"]))
                for line in report["errors"]:
                    log_line("update staging 清理失败一项: %s" % line, "WARNING")
            except Exception as exc:
                log_swallow("stock_float_window._prune_update_staging", exc)

        threading.Thread(target=worker, name="staging-prune", daemon=True).start()

    def _sweep_temp_copies(self):
        """后台清扫临时目录里本程序的可启动旧副本（只改名、不删除）。

        仅对冻结产物生效：源码运行与测试没有"旧副本"概念，且不该去动真机 /tmp。
        异常只记日志、不外抛（一次清扫会记多条：每个副本一条，失败再各一条）。

        `SF_ALLOW_TEMP_LAUNCH=1` 同时关闭本清扫（v1.4.24 复核 F2）：那个变量在
        sf_bootstrap 里的语义就是"我确实要把产物放临时目录核验"。核验者常常先把副本
        拷到 /tmp、还没双击启动，而"尚未启动"恰好不在 running_bundle_paths 的保护范围
        内 —— 没有这道开关，正在运行的正式版会在 45 秒后把那份待核验副本改名，
        核验者拿到的是"文件夹名字变了、打不开"。命令行入口 tools/sweep_temp_apps.py
        由人显式调用，不受该开关影响。
        """
        if getattr(self, "_threads_stopped", False):
            return
        if not getattr(sys, "frozen", False):
            return
        if os.environ.get("SF_ALLOW_TEMP_LAUNCH", "").strip().lower() in ("1", "true", "yes"):
            log_line("temp sweep skipped: SF_ALLOW_TEMP_LAUNCH 已放行核验")
            return

        def worker():
            try:
                import sf_temp_sweep
                changed = sf_temp_sweep.sweep_temporary_copies(log=log_line)
                if changed:
                    log_line("temp-copies sweep: neutralized %d" % changed)
            except Exception as exc:
                log_line("temp sweep skipped: %r" % (exc,), "WARNING")

        threading.Thread(target=worker, daemon=True, name="temp-sweep").start()

    def _anchor_update_dialog_screen(self):
        """把『该弹在哪块屏』交给系统弹窗模块：悬浮窗所在那块屏的中心点。

        v1.4.38（用户 2026-10-08 要求：检查更新相关的窗口一律出现在屏幕正中）。
        居中本身在 sf_system_alert 里做，这里只负责选屏 —— 本应用是悬浮窗程序，
        弹窗没有属主窗口可跟；副屏上用「检查更新」时若跟着主屏走，弹窗会落在用户
        没在看的那块屏正中，看起来就像点了没反应（与 v1.2.71 修编辑器跳屏同病根）。
        """
        try:
            if not isinstance(self, QWidget) or QApplication.instance() is None:
                return
            from PyQt6.QtGui import QGuiApplication
            scr = (QGuiApplication.screenAt(self.frameGeometry().center())
                   or QGuiApplication.primaryScreen())
            if scr is None:
                return
            import sf_system_alert
            center = scr.geometry().center()
            sf_system_alert.set_screen_anchor(center.x(), center.y())
        except Exception as exc:
            log_line("update dialog screen anchor failed: %r" % (exc,), "WARNING")

    def _center_on_own_screen(self, widget):
        """Qt 自绘框也摆到悬浮窗所在屏的正中（原生弹窗不可用时的兜底路径）。"""
        try:
            if not isinstance(self, QWidget) or QApplication.instance() is None:
                return            # 与 _anchor_update_dialog_screen 同一道守卫：
                                  # 宿主不是浮窗本体（测试替身、无 Qt 环境）时不动几何
            from PyQt6.QtGui import QGuiApplication
            scr = (QGuiApplication.screenAt(self.frameGeometry().center())
                   or QGuiApplication.primaryScreen())
            if scr is None:
                return
            area = scr.availableGeometry()
            widget.move(area.x() + (area.width() - widget.width()) // 2,
                        area.y() + (area.height() - widget.height()) // 2)
        except Exception as exc:
            log_line("center update dialog failed: %r" % (exc,), "WARNING")

    def _check_updates_background(self, manual=False):
        return _UPDATE_FLOW.check(self, manual)

    def _update_check_stale(self):
        return _UPDATE_FLOW.check_stale(self)

    def _show_checking_dialog(self):
        """「正在检查更新」框：只在还没有这张框时新建，重复点击不叠框。

        仅手动检查才弹：系统弹窗会把应用带到最前（activate），启动后的静默自检
        与切回程序的补查不得在看盘时抢焦点。这张框由回包时的 _close_update_flow_box
        统一收口 —— 包括「点击复用了别人的在飞检查」这一路，点了之后不会留一个
        关不掉的框。

        v1.4.44 起带「取消」：清单超时从 5 秒放宽到 12 秒 × 2 次之后，最坏要 24 秒
        才有答复，这期间用户应当能明确地不再等。等待框只保留「取消」，按下后
        立刻收框并作废这一次的答复。网络线程不打断（半途掐连接是另一类资源问题），
        它回来只更新状态与日志。
        """
        if not isinstance(self, QWidget) or QApplication.instance() is None:
            return
        import sf_system_alert
        self._anchor_update_dialog_screen()
        dialog = getattr(self, "_update_check_dialog", None)
        if dialog is not None:
            present = getattr(dialog, "present", None)
            if present is None or present():
                return
            self._dispose_update_dialog(dialog)
        token = object()
        self._update_check_dialog_token = token
        self._update_check_dialog = sf_system_alert.show_alert(
            title="软件更新", text="正在检查更新，请稍候…",
            buttons=("取消",), default="取消", cancel="取消",
            icon_path=sf_system_alert.bundled_icon_path(),
            on_result=lambda button: self._q_put(("update_check_cancel", (button, token))))

    def _handle_update_check_cancel(self, button):
        """等待框点击结果：收框，且「取消」作废这一次的答复；兼容旧框回调。"""
        if isinstance(button, tuple):
            button, token = button
            # A queued response from a replaced/finished dialog cannot cancel its successor.
            if token is not getattr(self, "_update_check_dialog_token", None):
                return
        self._dispose_update_dialog(getattr(self, "_update_check_dialog", None))
        if button == "取消":
            self._update_manual_wait = False
            self._update_check_aborted = True
            log_line("update check cancelled by user")

    def _show_update_message(self, title, text, detail=""):
        """一次性更新提示：macOS 系统弹窗，本进程无 Qt 窗口，退出期零析构竞态。"""
        import sf_system_alert
        if getattr(self, "_quitting", False):
            return
        self._dispose_update_dialog(getattr(self, "_update_message_dialog", None))
        body = text + ("\n\n" + detail if detail else "")
        self._anchor_update_dialog_screen()
        self._update_message_dialog = sf_system_alert.show_alert(
            title=title, text=body, buttons=("好",),
            icon_path=sf_system_alert.bundled_icon_path())

    def _dispose_update_dialog(self, dialog):
        """收口系统弹窗（terminate 后台 osascript）；重复调用安全。"""
        if dialog is None:
            return
        for attr in ("_update_message_dialog", "_update_prompt_dialog",
                     "_update_check_dialog"):
            if dialog is getattr(self, attr, None):
                setattr(self, attr, None)
                if attr == "_update_check_dialog":
                    self._update_check_dialog_token = None
        try:
            dialog.close()
        except Exception as exc:
            log_line("dispose update dialog failed: %r" % (exc,), "WARNING")

    def _schedule_update_retry(self):
        """静默检查失败后退避：第一次5分钟，后续每30分钟；不影响行情请求。"""
        timer = getattr(self, "_update_retry_timer", None)
        if timer is None or getattr(self, "_quitting", False):
            return
        delay = UPDATE_RETRY_FIRST_MS if self._update_failure_count == 1 else UPDATE_RETRY_LATER_MS
        timer.start(delay)

    def _check_updates_after_resume(self, state):
        """切回本程序 / Mac 从睡眠恢复后补查；仅在恢复到活跃状态时触发，且不抢焦点。

        v1.4.43 加节流。这个信号是「每次变为活跃」都发，alt-tab 一天几十次就是几十次
        请求；更糟的是每次都占住在飞标记最长一个网络往返，正好把用户随后点的
        「检查更新」撞进在飞窗口里（撞上了旧代码什么都不弹）。间隔取
        UPDATE_RESUME_MIN_GAP_SEC，来源与取值理由见 sf_constants 旁的说明。
        **必须用墙钟**：macOS 睡眠期间 time.monotonic 不前进，一小时睡眠醒来只会被算成
        「过了几秒」，那样节流会把真正该做的唤醒补查一并吞掉。
        """
        if state != Qt.ApplicationState.ApplicationActive:
            return
        since_last = time.time() - (getattr(self, "_update_last_check_at", 0.0) or 0.0)
        if since_last < UPDATE_RESUME_MIN_GAP_SEC:
            return
        self._check_updates_background()

    def _close_update_flow_box(self):
        """同步收口更新状态框，必须在主窗口与事件循环退出前执行。

        更新检查和下载都可能在网络线程仍未回传时退出。系统弹窗（含 v1.4.17 的
        原生进度框）只需 orderOut，close() 即收口；**Qt 自绘的下载进度框是降级
        兜底路径**，它带 QWindow，析构时机必须自己拿住：v1.4.17 起先撤销 Python
        引用再 close()+deleteLater()，仍不够 —— 排队的延迟删除会一路活到
        QApplication.quit() 触发的 exit()，在 C++ 静态析构阶段才被冲刷，那时
        QWindow/QSurface 销毁会踩到已释放的线程局部存储（v1.4.18 在 iMac 上的
        SIGSEGV 即此，见 _flush_deferred_deletions）。v1.4.20 起改为
        **close() + sip.delete() 同步销毁**，退出时不再留下任何待删的 Qt 窗口。
        重复调用安全。
        """
        self._update_check_dialog_token = None
        for attr in ("_update_check_dialog", "_update_message_dialog",
                     "_update_prompt_dialog"):
            dialog = getattr(self, attr, None)
            setattr(self, attr, None)
            if dialog is not None:
                try:
                    dialog.close()
                except Exception:
                    pass
        box = getattr(self, "_update_flow_box", None)
        self._update_flow_box = None
        self._update_flow_progress = None
        self._update_flow_cancel = None
        if box is None:
            return
        try:
            box.close()
        except Exception as exc:
            log_line("dispose download progress box failed: %r" % (exc,), "WARNING")
        if not isinstance(box, QWidget):
            return                      # 原生弹窗句柄没有 Qt 析构面，close() 即收口
        try:
            # v1.4.20：这里**必须同步销毁**，不能留 deleteLater() 排队。
            # QApplication.quit() 在 macOS 上等于 -[NSApplication terminate:] →
            # exit()，排队的 DeferredDelete 会被推到 C++ 静态析构阶段才执行，
            # 那时销毁这个带 QWindow 的框就是空指针崩溃（详见
            # _flush_deferred_deletions 的说明）。此刻 Qt/AppKit 均完整，
            # sip.delete 直接走正常析构。
            _sip_delete(box)
        except RuntimeError:
            # Qt 已先于 Python 包装对象销毁时，不能再触碰底层对象。
            pass
        except Exception as exc:
            log_line("dispose download progress box failed: %r" % (exc,), "WARNING")

    def _handle_update_check(self, manual, update, error):
        return _UPDATE_FLOW.checked(self, manual, update, error)

    def _ready_update_archive(self, update):
        return _UPDATE_FLOW.ready_archive(self, update)

    def _prompt_update(self, update):
        """新版确认改用 macOS 系统弹窗。

        手动检查仍是“立即更新 / 暂不更新”：用户确认后才开始下载。自动检查则在
        下载完成后进入“更新并重启应用 / 暂不更新”：已校验的 ZIP 会保留，稍后可复用。

        点击结果来自后台线程（osascript 子进程退出），必须经线程安全队列
        转投主事件循环处理，绝不在回调线程里触碰任何 Qt 对象。
        """
        import sf_system_alert
        import sf_update
        if getattr(self, "_quitting", False):
            return
        self._dispose_update_dialog(getattr(self, "_update_prompt_dialog", None))
        # 展示实际更新说明；并按发版阶段把 Beta 版显式标出来（"发现新 Beta 版本 vX"），
        # 稳定版保持原措辞。这是单 feed 的可见标签，不改变"照常更新"的行为。
        # v1.4.21：只展示**本次版本**那一段。清单里的 notes 历史上是整份
        # release_notes.txt（v1.4.20 那次一屏塞了 4 个版本），显示侧必须自己裁；
        # 发版侧同步改为只签名本次版本，两处任一生效即可，见 sf_update 说明。
        notes = sf_update.numbered_notes_for_display(update.notes, update.version)
        headline = sf_update.offer_headline(update.version, getattr(update, "stage", "stable"))
        ready_archive = self._ready_update_archive(update)
        ready_to_restart = ready_archive is not None
        buttons = ("暂不更新", "更新并重启应用") if ready_to_restart else ("暂不更新", "立即更新")
        primary = buttons[1]
        self._anchor_update_dialog_screen()
        self._update_prompt_dialog = sf_system_alert.show_alert(
            title="软件更新",
            text="%s\n\n%s" % (headline, notes),
            message=headline,
            info=notes,
            buttons=buttons,
            default=primary,
            cancel="暂不更新",
            icon_path=sf_system_alert.bundled_icon_path(),
            on_result=lambda button: self._q_put(("update_prompt_answer", button)))

    def _handle_update_prompt_answer(self, button):
        return _UPDATE_FLOW.answered(self, button)

    def _accept_update_prompt(self, box, update):
        """先完成检查提示框的关闭和销毁，再启动唯一的下载提示框。"""
        self._dispose_update_dialog(box)
        QTimer.singleShot(0, lambda info=update: self._download_update_background(info))

    def _download_update_background(self, update, prompt_when_ready=False, show_progress=True):
        return _UPDATE_FLOW.download(self, update, prompt_when_ready, show_progress)

    def _open_download_progress_box(self):
        """下载进度框：优先 macOS 原生弹窗 + 系统进度条，不可用时退回 Qt 自绘框。

        原生路径 v1.4.17 起为首选（用户要求弹窗一律用系统默认样式）；Qt 框必须
        原样保留：AppKit 不可用、不在主线程、或建窗失败时，"完全看不到进度"比
        "样式不够原生"严重得多 —— 用户会以为程序卡死。
        """
        import sf_system_alert
        self._anchor_update_dialog_screen()
        handle = sf_system_alert.show_progress_alert(
            message="正在下载更新…", info="准备中 0%",
            cancel="取消",
            on_result=self._on_download_dialog_answer,
            icon_path=sf_system_alert.bundled_icon_path())
        if handle is not None:
            self._update_flow_box = handle
            self._update_flow_progress = None
            self._update_flow_cancel = None
            return handle
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle("软件更新")
        box.setText("正在下载更新…")
        box.setStandardButtons(QMessageBox.StandardButton.NoButton)
        bar = QProgressBar(box)
        bar.setRange(0, 100)
        bar.setValue(0)
        box.layout().addWidget(bar, 1, 0, 1, box.layout().columnCount())
        cancel = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        cancel.clicked.connect(self._cancel_update_download)
        self._update_flow_box = box
        self._update_flow_progress = bar
        self._update_flow_cancel = cancel
        box.show()
        # v1.4.38：兜底框同样居中。必须 show() 之后再量 —— 没上过屏时
        # width()/height() 还是布局前的默认值，按它算中心会偏。
        self._center_on_own_screen(box)
        return box

    def _on_download_dialog_answer(self, button):
        """原生进度框的点击（AppKit 事件里，主线程）：只置停止标志。

        刻意不在这里丢弃框的引用 —— 下载回包时由 _close_update_flow_box 统一
        收口，避免在 AppKit 事件栈正在拆窗的同时再析构一次 NSWindow。
        """
        if button == "取消":
            self._cancel_update_download()

    def _cancel_update_download(self):
        return _UPDATE_FLOW.cancel_download(self)

    def _handle_update_progress(self, pct, downloaded, total):
        box = getattr(self, "_update_flow_box", None)
        if box is None:
            return
        value = max(0, min(100, int(pct)))
        label = "%d%%（%.1f / %.1f MB）" % (value, downloaded / 1048576, total / 1048576)
        set_progress = getattr(box, "set_progress", None)
        if set_progress is not None:
            try:
                set_progress(value, label)
                return
            except Exception as exc:
                # 原生刷新失败就退回自绘框写法：宁可样式退化，也不能没有进度反馈。
                log_line("native progress refresh failed: %r" % (exc,), "WARNING")
        bar = getattr(self, "_update_flow_progress", None)
        setter = getattr(box, "setText", None)
        if bar is None or setter is None:
            return                      # 原生框没有 setText：不硬凑，避免抛出
        bar.setValue(value)
        setter("正在下载更新… " + label)

    def _launch_downloaded_update(self, update, archive):
        """把已校验的 ZIP 交给助手并退出当前进程。"""
        try:
            import sf_update
            import uuid
            handoff_id = uuid.uuid4().hex
            launch = sf_update.launch_installer(archive, update.archive_sha256, update.version,
                                                handoff_id=handoff_id)
            session = vars(self).get("_runtime_session")
            session_id = vars(self).get("_runtime_session_id")
            if isinstance(session, RuntimeSessionLedger) and isinstance(session_id, str):
                try:
                    session.mark_update_handoff(session_id, handoff_id=handoff_id,
                                                expected_version=update.version)
                except Exception:
                    process = getattr(launch, "process", None)
                    if process is not None:
                        process.terminate()
                    sf_update.handoff_context_path(handoff_id).unlink(missing_ok=True)
                    sf_update.handoff_ack_path(handoff_id).unlink(missing_ok=True)
                    raise
        except Exception as exc:
            log_line("update handoff failed: %r" % (exc,), "ERROR")
            self._show_update_message("更新未完成",
                                      "无法启动安装助手。", "当前版本未受影响。")
            return
        self.quit()

    def _handle_update_download(self, update, archive, error):
        return _UPDATE_FLOW.downloaded(self, update, archive, error)

    def _face_guard_screen_off(self, now=None):
        """屏幕锁定/休眠判定，2 秒才向系统问一次（守卫轮询是 250ms 一拍）。

        读不到答案（框架缺失、查询抛错）时保持「没锁」—— 保护不许因为一次
        探测失败就静默停掉，这与 features.ini 的 fail-open 是同一个方向。
        """
        now = time.monotonic() if now is None else float(now)
        if now - self._face_guard_screen_probe_at >= FACE_GUARD_SCREEN_PROBE_SEC:
            self._face_guard_screen_probe_at = now
            try:
                self._face_guard_screen_locked = bool(sf_presence.screen_unavailable())
            except Exception as exc:
                log_swallow("stock_float_window._face_guard_screen_off", exc)
                self._face_guard_screen_locked = False
        return bool(self._face_guard_screen_locked)

    def _refresh_face_guard_menu_check(self):
        """菜单开着时让勾号跟真实状态走（v1.4.54，见菜单构造处那段说明）。

        值取 `_face_guard_switch_on()`：与点击目标同一个口径（v1.4.67 起）——
        勾号亮着的时候用户再点必须是"关"，所以刷新与点击不许各说各话。

        不看 `menu.isVisible()`：弹出路径与测试桩都可能不走真实显示，而定时器的
        生命周期由 `_on_ctx_menu_closed` 负责（关菜单即停），菜单对象没了也会在
        下一拍自停 —— 这里只负责"还挂着就刷真值"。
        """
        act = getattr(self, "_face_guard_menu_action", None)
        if act is None:
            self._stop_face_guard_menu_timer()
            return
        try:
            _on = self._face_guard_switch_on()
            act.setChecked(_on)
            # 图标与勾号**同一个口径、同一个刷新点**（v1.4.69）：有图标的行 Qt 不画勾，
            # 所以这一行"现在是开还是关"只由图标说话。菜单开着时状态自己翻转
            # （守卫收窗、起会话失败）也照样看得见，不会停在弹出那一刻的旧形状。
            _set_menu_icon(act, "eye_open" if _on else "eye_closed")
        except RuntimeError:
            # 菜单/动作的 C++ 对象已销毁（正常关闭路径），下一次回调自会停表。
            self._stop_face_guard_menu_timer()

    def _stop_face_guard_menu_timer(self):
        timer = getattr(self, "_face_guard_menu_timer", None)
        if timer is not None:
            try:
                timer.stop()
            except RuntimeError as exc:
                # 定时器随窗口一起被销毁时 stop() 会抛；不静默吞掉（AGENTS §6 第 9 条）。
                log_swallow("stock_float_window._stop_face_guard_menu_timer", exc)
        self._face_guard_menu_timer = None
        self._face_guard_menu_action = None

    def _on_ctx_menu_closed(self, closed_menu=None):
        """释放右键菜单（幂等）。

        v1.2.65 第1轮走查修复：QMenu 以 self 为父对象构造，Qt 的父子机制会让
        **父对象在自己的 children 列表里持有它**。仅把 Python 侧引用置 None 不会
        释放 C++ 对象 —— 每次右键都往窗口的子对象列表里多挂一个 QMenu（连带它
        的全部 QAction、样式表、原生 NSMenu 后端），直到进程退出才统一释放，
        属于长生命周期下的持续内存增长。必须 deleteLater() 显式销毁
        （延迟删除，不会在 aboutToHide 信号投递过程中直接析构，安全）。
        """
        # An old popup may deliver its hide signal after a replacement exists.
        # Only the popup that owns the current reference may clear its state.
        if closed_menu is not None and closed_menu is not getattr(self, "_ctx_menu", None):
            return
        # v1.4.54：勾号刷新定时器与菜单同寿命，关菜单必须一起停（QTimer 以 self
        # 为父对象，不停就是每次右键泄漏一只 300ms 的活定时器）。
        self._stop_face_guard_menu_timer()
        menu = getattr(self, "_ctx_menu", None)
        self._ctx_menu = None
        if menu is None:
            return
        try:
            menu.deleteLater()
        except RuntimeError:
            pass   # C++ 对象已被销毁（异常关闭路径），忽略

    def _face_guard_stop_evidence(self):
        """最近一次停守卫的原因 + 距今多久；没停过就是 {}。

        单独成方法有两个理由：一是 `state` 归 off 之后"为什么停"只剩这一条通路
        （缩小 / 关功能 / 采不到画面 / 锁屏 / 退出，五种后果完全不同）；二是这里
        要把 dict 里的 `at` 当浮点用，就地写在字面量里 mypy 只能推成 object。
        """
        rec = self._face_guard_last_stop or {}
        raw_at = rec.get("at")
        at = float(raw_at) if isinstance(raw_at, (int, float)) else 0.0
        if at <= 0.0:
            return {}
        return {"reason": str(rec.get("reason") or ""),
                "age_sec": round(max(0.0, time.time() - at), 1)}

    def _face_guard_streak_age(self, which):
        """迟滞起点距今多久（秒）；还没开始计就返回 None。

        `which` 只认 "two" / "one"：分别是"连续看见两张脸"与"连续看见一张脸"的
        起点时刻（单调钟）。"客人坐了两秒才收"要看的是这根进度，而不是阈值表本身。
        """
        arb = self._face_guard_arbiter
        stamp = getattr(arb, "_two_since" if which == "two" else "_one_since", None)
        if stamp is None:
            return None
        return round(max(0.0, time.monotonic() - float(stamp)), 2)

    def _alert_code_histogram(self):
        """存储里每条目标价各归属于哪只股票的计数（"" = 无主，按顶层股票处理）。

        只回计数不回价格 —— 目标价数值本身已经在 config.json 里，这里补的是
        **归属分布**：v1.4.59/60 那串"切股之后目标价整片消失"的现场，光看总条数
        分不清"用户自己删了"还是"被盖章成别的股票带走了"。
        超过 20 只按条数取前 20 并标 truncated，避免异常存储把包撑大。
        """
        counts: dict = {}
        for alert in load_alerts():
            key = (str(alert.get("code") or "") if isinstance(alert, dict) else "?") or ""
            counts[key] = counts.get(key, 0) + 1
        if len(counts) <= 20:
            return counts
        top = sorted(counts.items(), key=lambda kv: -kv[1])[:20]
        out = dict(top)
        out["truncated"] = len(counts) - 20
        return out

    def _tool_window_geometries(self):
        """工具窗（目标价面板 / 录入窗）此刻的可见性与几何。

        为什么必须单列（1.4.28 那一族）：这类投诉是"面板留在原地 / 跑到屏幕外 /
        看不见点不动"—— 属于**位置错而不是异常**，`log_swallow` 与 last_failures
        天生抓不到，包里此前只有主窗一只框。读的是公开几何 API，不碰私有状态。
        """
        out: dict = {}
        for key, attr in (("panel", "_target_panel"), ("editor", "_editor")):
            win = getattr(self, attr, None)
            if win is None:
                out[key] = {"present": False}
                continue
            try:
                out[key] = {"present": True, "visible": bool(win.isVisible()),
                            "x": int(win.x()), "y": int(win.y()),
                            "width": int(win.width()), "height": int(win.height())}
            except Exception as exc:
                # C++ 对象已销毁（关闭路径）：如实写出"读不到"，不写"没有这个窗"
                out[key] = {"present": True, "error": repr(exc)[:120]}
        return out

    def _screen_geometries(self):
        """当前所有屏幕的几何与主屏标记（1.4.38/1.4.45 的未验证项：副屏落点）。

        吸附"够不着就静默不动"、更新弹窗落在用户看不见的那块屏，都只有在这里
        留下可用区才有解释 —— 光有主窗坐标看不出"它其实已经贴到第二屏边缘"。
        """
        app = QApplication.instance()
        out: list = []
        if not isinstance(app, QApplication):
            return out
        screens = app.screens()
        primary = app.primaryScreen()
        for scr in screens:
            g = scr.geometry()
            av = scr.availableGeometry()
            out.append({"name": scr.name(),
                        "primary": scr == primary,
                        "geometry": [g.x(), g.y(), g.width(), g.height()],
                        "available": [av.x(), av.y(), av.width(), av.height()]})
        return out

    def _export_diagnostics(self, background=False, *, prepare_only=False,
                            feedback_context=None):
        """导出诊断包：config + 日志 + 近期崩溃报告（含正文关键段）打包为 zip 到桌面，
        便于反馈问题时快速定位原因（可观测性）。

        v1.2.42：修复可观测性缺口 —— 崩溃报告不再只列"文件名+时间"，
        把 ~/Library/Logs/DiagnosticReports 下本应用（Skyrocket*，兼容旧版 StockFloatTargetPrice*）
        最近 7 天内优先的 1~2 份报告的关键段（异常类型/终止原因/崩溃线程与主线程
        堆栈/二进制镜像关键行，每份 ≤200KB）写入 zip 的 crash_reports/ 目录，
        原"文件名+时间"清单保留在 diagnostics.txt 作索引。诊断包直接写入桌面，不创建外层文件夹。"""
        if background and getattr(self, "_diagnostics_exporting", False):
            return None
        out = None
        temp_out = None
        try:
            from sf_diagnostic_capture import DiagnosticSources, prepare_diagnostics
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            requested = os.path.join(os.path.expanduser("~/Desktop"),
                                     "%s-诊断-%s.zip" % (APP_DISPLAY_NAME, ts))
            output_dir = os.path.dirname(requested)
            if not prepare_only:
                os.makedirs(output_dir, exist_ok=True)
                fd, temp_out = tempfile.mkstemp(
                    prefix=".%s-" % os.path.basename(requested), suffix=".tmp", dir=output_dir)
                os.close(fd)
            sources = DiagnosticSources(
                config_path=CONFIG_PATH,
                config_backup_path=CONFIG_BACKUP_PATH,
                last_quote_path=LAST_QUOTE_PATH,
                log_path=LOG_PATH,
                net_monitor=_NET_MONITOR,
                get_last_http_status=get_last_http_status,
                get_last_quote_source=get_last_quote_source,
                degraded_source_streak=degraded_source_streak,
                load_alerts=load_alerts,
                beijing_now=_beijing_now,
                expired=expired,
                store_prune_report=store_prune_report,
                get_last_minute_source=get_last_minute_source,
                running_bundle_id=_running_bundle_id,
                bundle_version=_bundle_version,
                peek_install_result=_peek_install_result,
                menu_icon_evidence=_menu_icon_evidence,
                feature_snapshot=feature_snapshot,
                font_diagnostics=font_diagnostics,
                paint_diagnostics=paint_diagnostics,
                notify_delivery_stats=_notify_delivery_stats,
                calendar_sync_status=calendar_sync_status,
                last_failures_path=last_failures_path,
                update_inventory=_update_inventory,
                is_crash_report_name=is_crash_report_name,
                select_crash_reports=select_crash_reports,
                extract_crash_key_sections=extract_crash_key_sections,
                snap_margin=SNAP_MARGIN,
                json=json,
                log_max=LOG_MAX_BYTES,
                log_keep=LOG_ROTATE_KEEP,
                bundle_id=BUNDLE_ID,
            )
            z = prepare_diagnostics(self, temp_out, sources, feedback_context=feedback_context)
            if prepare_only:
                return z
            assert temp_out is not None
            if background:
                worker = self._diagnostics_controller.start(
                    z, temp_out, requested, self.diagnostics_ready.emit)
                if worker is None:
                    return None
                temp_out = None
                QToolTip.showText(self.mapToGlobal(QPoint(self.width() // 2, 10)),
                                  "正在导出诊断信息...", self)
                return None
            z.finish()
            out = _publish_diagnostics_file(temp_out, requested)
            log_line("diagnostics exported -> %s" % out)
            try:
                import subprocess
                subprocess.Popen(["/usr/bin/open", "-R", out])
            except Exception:
                pass
            try:
                QToolTip.showText(self.mapToGlobal(QPoint(self.width() // 2, 10)),
                                  "诊断信息已导出：%s" % out, self)
            except Exception:
                pass
            return out
        except Exception as exc:
            if prepare_only:
                raise
            log_line("diagnostics export error: %r" % (exc,), "ERROR")
            try:
                QToolTip.showText(self.mapToGlobal(QPoint(self.width() // 2, 10)),
                                  "诊断信息导出失败，请查看日志", self)
            except Exception:
                pass
            return None
        finally:
            if temp_out:
                try:
                    os.unlink(temp_out)
                except OSError as exc:
                    log_line("diagnostics temporary cleanup error: %r" % (exc,), "WARNING")

    def _finish_diagnostics_export(self, archive, temporary, requested):
        self._diagnostics_controller.finish_sync(
            archive, temporary, requested, self.diagnostics_ready.emit)

    def _on_diagnostics_ready(self, result):
        self._diagnostics_exporting = False
        self._diagnostics_thread = None
        if not self.running:
            return
        output, error = result
        message = "诊断信息导出失败，请查看日志" if error else "诊断信息已导出：%s" % output
        if output:
            log_line("diagnostics exported -> %s" % output)
            try:
                import subprocess
                subprocess.Popen(["/usr/bin/open", "-R", output])
            except OSError as exc:
                log_swallow("stock_float_window.reveal_diagnostics", exc)
        QToolTip.showText(self.mapToGlobal(QPoint(self.width() // 2, 10)), message, self)

    def open_editor(self):
        try:
            # 处理旧弹窗：可见则聚焦复用；不可见/悬空则清理后重建
            if self._editor is not None:
                try:
                    if self._editor.isVisible():
                        # 弹窗已打开时再次点击编辑：原地关闭（开关行为）
                        self._editor.close()
                        return
                except RuntimeError:
                    pass  # 旧对象已被 C++ 删除，忽略并继续走清理
                try:
                    self._editor.destroyed.disconnect()
                except Exception:
                    pass
                try:
                    self._editor.deleteLater()
                except RuntimeError:
                    pass
                self._editor = None
            ed = EditorWindow(self.code, self.apply_stock, self.pos(), self.width())
            ed.set_surface_owner(self)
            self._editor = ed

            def _clear(_destroyed=None, obj=ed):
                # 身份校验：仅当当前引用仍是该弹窗时才清空，避免误清新对象
                if self._editor is obj:
                    self._editor = None
            ed.on_closed = _clear          # 关闭时同步清引用（主路径）
            ed.destroyed.connect(_clear)   # 兜底：真正销毁时也清引用
            ed.show()
            self._raise_tool_window(ed)
            ed.activateWindow()
            try:
                from AppKit import NSApp
                NSApp.activateIgnoringOtherApps_(True)
            except Exception:
                pass
        except Exception as exc:
            log_line("open_editor error: %r" % (exc,))
            self._editor = None

    def apply_stock(self, code):
        """切换关注股票（v1.4.59 起：这一步**不再删目标价**）。

        旧行为是无条件 `_clear_targets()` —— 上一只股票的目标价整片消失、无确认、
        无撤销，而同一个面板点「清空」反而有两次确认（S4，P1 数据丢失）。
        现在存储按 code 分家（sf_config.save_alerts 只替换当前股票的条目），
        切走再切回来，那几条还在；因此也不需要确认框了 —— 没有东西被删，
        弹框只是噪音（本 app 的规矩：不加解释性弹窗）。
        """
        transition = self._stock_transition
        content = None
        if self.isVisible() and code != self.code:
            try:
                content = transition.frame(self._stock_content_image())
            except Exception as exc:
                log_swallow("stock_float_window.stock_transition.capture", exc)
        result = self._apply_stock_now(code)
        if result and content is not None:
            transition.start(content)
            self._partial_price = None
            self.update()
        return result


    def _apply_stock_now(self, code):
        # 切股后停掉上一只股票的破位闪烁定时器并复位相位，避免旧状态残留影响新股票显示
        try:
            self._flash_timer.stop()
        except Exception:
            pass
        # 切股时复位破位、下跌和目标价彩虹提醒，避免旧股票视觉状态残留到新股票。
        self._anim_driver.reset_flash()
        self._anim_driver.reset_rainbow()
        try:
            self._rainbow_timer.stop()
        except Exception:
            pass
        # 12.x v1.2.11：切股时同时复位下跌闪烁相位与各档冷却记录，避免残留影响新股票
        try:
            self._flash_drop_timer.stop()
        except Exception:
            pass
        # v6：切股清空「目标价」（旧股票目标不适用新股票）+ 复位彩虹 + 停定时器
        # 轻微项修复：关闭面板原本被关在 targets 功能开关内部 —— 功能关闭时若
        # 仍有残留面板（如运行中关闭过功能），切股后它会继续显示为上一只股票的
        # 死面板。面板收口是生命周期问题，与功能是否启用无关，移到门控外。
        try:
            self._close_target_panel()
        except Exception:
            pass
        self.code = code
        ticks = getattr(self, "_target_ticks", None)
        if ticks is not None:
            ticks.clear()
        # v1.4.59（S4 彻底解）：换完 code 立刻把**这只股票自己的**目标价接回来。
        # 存储里可能还留着它（今天早些时候设的），也可能什么都没有（第一次看这只）。
        self._adopt_targets_for(code)
        self.quote = None
        self.minute_points = []
        # v1.4.18 走查（S1）：切股必须复位"目标价判定价"。_real_price/_price_is_fallback
        # 只在行情回调（_apply_quote）里写，切股时若不清零，收盘后/熔断开新行情迟迟不到，
        # _current_price() 会直接返回**上一只股票**的缓存价 —— 目标价面板的"当前价"、
        # 涨到/跌到校验、越线判定全用别家股票的价格（可长达一小时）。一并复位动画驱动价，
        # 否则此期间收展/拖高图表会让 _disp_price 被旧价回填，主窗不再显示 --。
        self._real_price = 0.0
        self._price_is_fallback = True
        try:
            drv = self._anim_driver
            drv.anim_from_price = drv.anim_to_price = 0.0
            drv.anim_from_pct = drv.anim_to_pct = 0.0
        except Exception:
            pass
        # v1.4.7：切股即"无当日数据"，复位当日性与滞后标记（新股票重新计时）。
        self._minute_day = ""
        self._minute_stale = False
        self._minute_last_ok_at = 0.0
        self._minute_fail = 0
        self._width_content_key = None
        self._width_dirty = True
        self._line_ver += 1   # 清空分时数据：驱动折线缓存失效，避免旧图残留
        self._disp_price = None
        self._disp_pct = None
        self._pct_unknown = False   # v1.3.49：切股重置，避免上一只股票的未知态残留
        self._anim_timer.stop()
        self._roll_pending = {}   # v1.2.25：切股时清除逐位滚动目标与状态，避免残留到新股票
        self._roll_prev = {}
        self._roll_anim = {}
        self._after_save_config(save_config(code, self.x(), self.y(), self.height(), self._bg_cfg))
        self.update()
        self._force_fetch()
        return True

    # ---------- 数据 ----------
    def _after_save_config(self, ok):
        """v1.2.39：配置保存失败不再静默 —— 失败时弹一次性气泡提示用户
        （磁盘空间 / config 文件权限问题），并记录 WARNING 日志。"""
        if ok:
            # v1.4.10：记下"当前坐标已落盘"，供 _clamp_to_screen 的写盘去重判断。
            # 必须在这里统一更新而不是只在钳制分支更新：拖拽/切股等路径也会改坐标，
            # 若钳制处记的是旧值，就会把一次真正需要写的盘跳掉。
            try:
                self._clamped_saved_pos = (self.x(), self.y())
            except Exception:
                pass
            return
        try:
            log_line("config save failed; notify user", "WARNING")
            from PyQt6.QtCore import QPoint, QTimer
            from PyQt6.QtWidgets import QToolTip
            g = self.mapToGlobal(QPoint(self.width() // 2, 10))
            QToolTip.showText(g, "配置保存失败：请检查磁盘空间或 config 文件权限", self)
            QTimer.singleShot(5000, QToolTip.hideText)
        except Exception:
            pass

    def _force_fetch(self):
        """v1.2.71：请求"立刻拉一次"行情与分时。

        置位标志并唤醒两条常驻线程；在途请求不取消，完成后再消费合并的刷新请求。
        连续点击「刷新行情」/ 快速连切股票不会无上限建线程或堆积请求。
        常驻线程已异常退出时重建并登记循环，保证刷新能力不因线程死亡丢失。
        """
        # 退出后不再接受排队到达的刷新请求，避免停止循环后又重建后台线程。
        if getattr(self, "_threads_stopped", False):
            return
        with self._force_lock:
            self._force_quote = True
            self._force_minute = True
        # v1.4.10：线程现在可能睡到 5 秒之外，必须显式叫醒，否则"刷新行情"
        # 会真的等一个睡眠周期才生效（原实现靠 0.1s 节拍兜住）。
        self._resched_loops()
        # 锁外到重建线程前，退出信号可能刚好抵达；再次检查消除该竞态。
        if getattr(self, "_threads_stopped", False):
            return
        # v1.2.72 第6轮走查：常驻循环已死时，旧逻辑每次强制刷新都只新建
        # 一条临时 fetch 线程，却不登记新的常驻线程；下一次点击仍判定"已死"，
        # 连续点击又继续建两条线程。此处必须重建并登记 wrapper，后续调用立刻
        # 看到 is_alive()，同一故障期最多恢复一组后台循环。
        qt = getattr(self, "_quote_thread", None)
        if qt is None or not qt.is_alive():
            qt = threading.Thread(target=self._quote_loop_wrapper,
                                  daemon=True, name="quote-loop-recover")
            self._quote_thread = qt
            qt.start()
        mt = getattr(self, "_minute_thread", None)
        if mt is None or not mt.is_alive():
            mt = threading.Thread(target=self._minute_loop_wrapper,
                                  daemon=True, name="minute-loop-recover")
            self._minute_thread = mt
            mt.start()

    def _take_force(self, name):
        """取走并清除一个"立即拉取"标志（供常驻线程在节拍内消费，消费即失效）"""
        with self._force_lock:
            if not getattr(self, name, False):
                return False
            setattr(self, name, False)
            return True

    def _is_trading_time(self, now=None):
        """A股连续竞价时段：统一复用模块级实现，消除双份逻辑"""
        return _is_trading_time(now)

    def _market_status(self, now=None):
        """非交易时段返回 (提示文案, 颜色)：统一复用模块级实现，便于单测覆盖"""
        return market_status(now)

    def _quote_loop_wrapper(self):
        """5.1 v1.2.27：行情线程入口。子线程内异常记录日志并带频率限制自动恢复
        （30 秒内最多重启 1 次，防死循环）；不改变 _quote_loop 原有语义。"""
        last_crash = 0.0
        while self.running:
            try:
                self._quote_loop()
                return
            except Exception as exc:
                now = time.monotonic()
                # v1.2.95 第6轮走查：退避必须用可唤醒的 _sleep，不能用
                # time.sleep —— 后者在退出时要硬等满 30 秒，_shutdown_threads
                # 的 join 超时 1.5s 必然落空，线程带着未完成的请求被进程强杀。
                if last_crash and now - last_crash < 30.0:
                    log_line("quote loop crashed too often, backing off 30s: %r" % (exc,), "ERROR")
                    if not self._sleep(30.0):
                        return                 # 退出中：立即结束，不等满退避
                log_line("quote loop exception, auto-restarting: %r" % (exc,), "ERROR")
                last_crash = time.monotonic()  # 退避之后再计时，频率限制才有意义

    def _quote_loop(self):
        """常驻行情线程：交易时段按熔断状态机间隔（0.1s/1s/5s）拉取；
        非交易时段降为 POLL_INTERVAL_OFF_MARKET（60 分钟）拉一次。
        非交易不高频空转，但保留低频拉取，保证启动/收盘后仍能显示最新收盘价（避免"加载中"）。"""
        # v1.2.40：到期判定/长暂停退避/节拍调度下沉 sf_quote_loop.QuoteLoopSchedule
        sched = QuoteLoopSchedule()
        while self.running:
            # v1.2.71：用单调时钟做到期判定。墙钟 time.time() 会被 NTP 校时、
            # 手动改表、夏令时切换拨回 —— 一旦回拨，`now - self.last` 变负，
            # 判定"未到期"会一直成立，行情静默停止刷新直到时钟追回原值。
            now = time.monotonic()
            interval = self._poll_interval()
            if self._minimized:      # v1.2.39：缩到 Dock 隐藏期间低频拉取（防后台空转耗电）
                interval = max(interval, POLL_INTERVAL_MINIMIZED)
            fetch, backoff, _tick = sched.step(now, interval)
            # v1.2.71：外部强制拉取（切股/刷新行情/Dock 恢复）—— 本节拍立即拉一次
            if self._take_force("_force_quote"):
                fetch, backoff = True, False
                sched.mark(now)
            if fetch:
                # 休眠唤醒/长时间暂停后首拉退避 1s，避免恢复瞬间多个请求洪峰
                # （last==0 首启不判长暂停，判定逻辑见 sf_quote_loop 模块 docstring）
                # v1.2.95 第6轮走查：同样改为可唤醒等待，退出时不必硬等这 1 秒。
                if backoff and not self._sleep(1.0):
                    return
                self._fetch_quote()
            # v1.4.10：睡到"下一次真正该决策"的时刻，替代恒定 0.1s 空转节拍。
            # 盘中 interval=0.1s 时 _nap 仍返回 0.1s，拉取节奏逐次不变。
            if not self._sleep(self._nap(sched, now, interval)):
                return

    def _poll_interval(self):
        """5.5 v1.2.5：返回当前轮询间隔（秒）。交易时段随熔断状态退避；
        v1.2.23：非交易时段固定 60 分钟低频（熔断不适用），仅保最新收盘价。
        v1.2.39：熔断→间隔映射下沉到 sf_scheduler.compute_poll_interval
        （上帝类拆分第一步），本方法仅保留薄委托与交易时段判定的壳。"""
        if not self._is_trading_time():
            return POLL_INTERVAL_OFF_MARKET
        if not feature_enabled("poll_backoff"):
            return compute_poll_interval(POLL_NORMAL)   # 关闭熔断 = 固定高频 0.1s
        with self._poll_lock:   # v1.2.22：跨线程读需持锁
            st = self._poll_state
        return compute_poll_interval(st)

    def _on_poll_result(self, ok, via_fallback=False):
        """5.5 v1.2.5：轮询结果驱动熔断状态机（仅交易时段由 _fetch_quote 调用）。
        成功：失败计数清零并回退 NORMAL；失败：连续 3 次 NORMAL->DEGRADED，
        连续 5 次 DEGRADED->OPEN。状态切换记录日志便于排查网络抖动。
        v1.2.69：via_fallback=True 表示本次数据来自备用源（主源故障），即使界面有
        数据也按失败驱动熔断，使主源单点故障时仍能降频、避免请求风暴。"""
        if not feature_enabled("poll_backoff"):
            return
        try:
            with self._poll_lock:   # v1.2.22：状态/计数跨线程读写统一持锁
                # v1.2.40：状态转移判定下沉 sf_quote_loop.next_poll_backoff（纯函数），
                # 原 _switch_poll_state 的落值+日志职责已内联合并，本类不再保有该方法
                # v1.2.69：备源兜底成功时按失败驱动熔断（via_fallback），使主源故障也能降频
                breaker_ok = ok and not via_fallback
                new_state, new_fail = next_poll_backoff(
                    self._poll_state, self._poll_fail, breaker_ok,
                    POLL_FAIL_TO_DEGRADED, POLL_FAIL_TO_OPEN)
                if new_state == self._poll_state and new_fail == self._poll_fail:
                    return
                old = self._poll_state
                self._poll_state = new_state
                self._poll_fail = new_fail
                # 与原实现一致：仅状态实际发生切换时才记日志（成功清零失败计数
                # 但状态本就是 NORMAL 时不刷 INFO）
                if new_state != old:
                    # v1.4.10：档位变了 → 轮询间隔变了，叫醒循环立刻按新间隔决策。
                    # 恢复（OPEN/DEGRADED→NORMAL）尤其重要：否则要等旧档的长睡结束
                    # 才提速，比原 0.1s 节拍慢。
                    self._resched_loops()
                    if new_state != POLL_NORMAL:
                        log_line("poll backoff: state %s -> %s (fail=%d)" % (old, new_state, new_fail), "WARNING")
                    else:
                        log_line("poll backoff: state %s -> %s (recovered)" % (old, new_state), "INFO")
        except Exception:
            pass

    def _minute_loop_wrapper(self):
        """5.1 v1.2.27：分时线程入口，与 _quote_loop_wrapper 相同策略（异常记日志 +
        30 秒内最多重启 1 次的频率限制，防死循环）。不改变 _minute_loop 原有语义。"""
        last_crash = 0.0
        while self.running:
            try:
                self._minute_loop()
                return
            except Exception as exc:
                now = time.monotonic()
                # v1.2.95 第6轮走查：同 _quote_loop_wrapper —— 退避改为可唤醒等待，
                # 否则退出时 join 必然超时，线程被硬杀。
                if last_crash and now - last_crash < 30.0:
                    log_line("minute loop crashed too often, backing off 30s: %r" % (exc,), "ERROR")
                    if not self._sleep(30.0):
                        return
                log_line("minute loop exception, auto-restarting: %r" % (exc,), "ERROR")
                last_crash = time.monotonic()

    def _minute_loop(self):
        """常驻分时线程：交易时段按 _minute_interval（展开 1s / 收起 30s，5.4 省电省流）；
        v1.2.23：非交易时段降为 POLL_INTERVAL_OFF_MARKET（60 分钟）拉一次；
        v1.2.30：拉取失败（fetch 异常或空返回）进入短期快速重试窗口，以
        MINUTE_RETRY_INTERVAL 间隔连续重试，拿到有效数据立即恢复正常周期；
        持续失败超过 MINUTE_RETRY_MAX 次后回落常规周期（防空转刷屏）。
        防重入（_fetch_minute 返回 None）不参与成败判定，原周期行为保持不变。"""
        # v1.2.40：到期判定/长暂停退避/节拍调度下沉 sf_quote_loop.QuoteLoopSchedule；
        # 快速重试窗口状态转移下沉 sf_quote_loop.minute_retry_state（纯函数）
        sched = QuoteLoopSchedule()
        retry_left = 0        # >0：处于快速重试窗口，剩余可重试次数
        hot = False           # 已进入持续失败回落状态（避免耗尽窗口后反复重置快速重试）
        retry_code = getattr(self, "code", None)
        while self.running:
            code = getattr(self, "code", None)
            if code != retry_code:
                retry_left, hot, retry_code = 0, False, code
                self._minute_retry_left, self._minute_retry_hot = 0, False
            # v1.2.71：单调时钟（理由同 _quote_loop，墙钟回拨会静默停拉）
            now = time.monotonic()
            base = self._minute_interval if self._is_trading_time() else POLL_INTERVAL_OFF_MARKET
            if retry_left > 0:
                # 快速重试只许比常规更快，不许更慢（修一处倒挂）：MINUTE_RETRY_INTERVAL
                # 定下来时常规周期还是 10s，后来展开态降到 CHART_EXPANDED_MINUTE_INTERVAL
                # =1.0s 而它没跟着调 —— 于是"快速重试"恰恰在用户唯一看得见分时图的那一档
                # （展开态）把节奏放慢成常规的 2.5 倍。取 min 之后：展开态仍是 1.0s（不再
                # 被拖慢），收起态 2.5s（比 30s 快 12 倍），盘后 2.5s（原样）。
                interval = min(MINUTE_RETRY_INTERVAL, base)
            else:
                interval = base
            if self._minimized:      # v1.2.39：缩到 Dock 隐藏期间低频拉取（防后台空转耗电）
                interval = max(interval, POLL_INTERVAL_MINIMIZED)
            fetch, backoff, _tick = sched.step(now, interval)
            # v1.2.71：外部强制拉取（切股/刷新行情/Dock 恢复）—— 本节拍立即拉一次
            if self._take_force("_force_minute"):
                fetch, backoff = True, False
                sched.mark(now)
            if fetch:
                # 休眠唤醒/长时间暂停后首拉退避 1s（判定见 sf_quote_loop 模块 docstring）。
                # 与行情线程一致，必须可被退出信号立即唤醒，避免关闭窗口时遗留分时线程。
                if backoff and not self._sleep(1.0):
                    return
                result = self._fetch_minute()
                if result is not None:
                    # v1.2.40：快速重试窗口状态转移（纯函数，行为与原内联分支逐位一致）
                    retry_left, hot = minute_retry_state(retry_left, hot, result, MINUTE_RETRY_MAX)
                    # 诊断留痕（S52）：窗口状态留给主线程读，见 __init__ 同名注释
                    self._minute_retry_left = retry_left
                    self._minute_retry_hot = hot
                    # Use the new retry state for this sleep, not one iteration later.
                    interval = min(MINUTE_RETRY_INTERVAL, base) if retry_left > 0 else base
                    if self._minimized:
                        interval = max(interval, POLL_INTERVAL_MINIMIZED)
                    # 窗口已耗尽仍失败：保持 hot 回落常规周期，不再重复进入快速重试
            # v1.4.10：分时线程是空转最严重的一条（展开 1s / 收起 30s 的间隔，
            # 却按 0.1s 节拍轮询，实测空转 93.8%~99.7%）。改为睡到下一次真正到期，
            # 到期时刻逐次不变。
            if not self._sleep(self._nap(sched, now, interval)):
                return

    def _wake_ui(self):
        """通知主线程"队列里有新东西，立刻来取"（v1.4.10 功耗）。

        与 self.timer 的 1 秒看门狗是互补关系：本方法是正常路径，看门狗是兜底。
        emit 失败（窗口已销毁/退出中）绝不冒到调用方 —— 数据已经在队列里，
        最迟一个看门狗周期也会被消化，不能因为唤醒失败让行情线程崩掉。
        """
        if not self.running:
            return
        try:
            self.ui_wakeup.emit()
        except Exception:
            pass

    def _q_put(self, item):
        """有界队列写入：队列满（UI 短暂卡顿）时丢弃最旧行情，保证最新数据可达，
        避免无界积压造成内存增长。行情/分钟线均为可丢失的实时数据，丢弃旧值是安全的。

        v1.4.10：入队即唤醒 UI，取代"UI 每 16ms 定时来排一次"。"""
        if item[0] == "quote":
            code, quote = item[1]
            ticks = getattr(self, "_target_ticks", None)
            if ticks is not None:
                ticks.add(code, quote)
        try:
            self.q.put_nowait(item)
        except queue.Full:
            try:
                self.q.get_nowait()  # 丢最旧
            except queue.Empty:
                pass
            try:
                self.q.put_nowait(item)
            except queue.Full:
                pass
        self._wake_ui()

    def _sleep(self, sec):
        """休眠等待：Event.wait 实现，quit 时 set _wake 立即退出（消除忙等）。

        v1.4.10 功耗：睡眠长度由 _nap() 按需算出（原来恒为 0.1s）。既然可能一睡
        几十秒，调度状态变更必须分别唤醒行情和分时；非循环调用保留 _resched 兜底。
        _shutdown_threads 会同时 set 两者，所以退出路径不会因睡得久而卡 join。
        """
        if self._wake.is_set():
            return False
        cur = threading.current_thread()
        ev = self._resched
        if cur is getattr(self, "_quote_thread", None):
            ev = getattr(self, "_quote_resched", ev)
        elif cur is getattr(self, "_minute_thread", None):
            ev = getattr(self, "_minute_resched", ev)
        if ev.wait(sec):
            ev.clear()
        return self.running

    def _resched_loops(self):
        """叫醒两条常驻循环，让它们立刻按新状态重新决策（v1.4.10 功耗）。

        只在"按需休眠"之后才需要：原来 0.1s 节拍天然会在 100ms 内发现任何变化，
        现在可能睡到 5 秒之外，所以每个改变轮询决策的动作都必须显式登记一次。
        用 getattr 兜底：构造早期/测试里替身窗口可能还没有这个事件。
        """
        for name in ("_resched", "_quote_resched", "_minute_resched"):
            ev = getattr(self, name, None)
            if ev is None:
                continue
            try:
                ev.set()
            except Exception as exc:
                log_swallow("FloatWindow._resched_loops", exc)

    def _nap(self, sched, now, interval):
        """本轮该睡多久（秒）。见 QuoteLoopSchedule.nap_seconds 的口径说明。

        盘中先短路：行情线程 10Hz 时 due_in 恒 ≤ 一个节拍，nap 一定等于节拍本身，
        边界算出来也不会参与 min —— 那就别白算。这个判断同时把"盘中拉取节奏
        逐次不变"变成代码里可以直接看出来的事实，而不是靠数值巧合相等。

        时段边界只在真要长睡时才算：盘后轮询间隔是 3600s，若不卡边界就会睡过
        09:30 开盘。边界算不出来（异常）时传 0，nap_seconds 会退回"固定节拍"的
        旧行为 —— 宁可多醒几次，也不能漏掉开盘。
        """
        try:
            if (sched.last + interval) - now <= sched.tick_seconds:
                return sched.tick_seconds
        except Exception:
            pass
        try:
            boundary = seconds_to_session_change()
        except Exception:
            boundary = 0.0
        try:
            return sched.nap_seconds(now, interval, boundary)
        except Exception:
            return getattr(sched, "tick_seconds", 0.1)

    def _fetch_quote(self):
        # 防重入：上一请求未完成则跳过，避免堆积
        if not self._quote_lock.acquire(blocking=False):
            return
        try:
            try:
                # 请求发出前冻结代码：网络阻塞期间用户可能已切换股票，回包必须
                # 带着旧代码回主线程，由 _poll 丢弃，不能在回包后再读 self.code。
                request_code = self.code
                q = ensure_current_quote(request_code, fetch_quote(request_code))
                if q:
                    # v1.2.72 第6轮走查：行情回包也携带请求发出时的代码。
                    # 分时此前已做此校验；行情只塞裸 q，A 股可用回包 code 兜底，
                    # 但美股/港股的非 6 位代码无法走 _normalize_code_6，切股后迟到
                    # 回包会短暂覆盖新股票。消费端统一按 request_code 丢弃旧回包。
                    # v1.2.69：备源兜底成功 ≠ 主源健康；主源故障时仍按失败计入熔断，
                    # 避免交易时段每 0.1s 反复打已死的主源（请求风暴）。
                    via_fallback = (get_last_quote_source() == "sina")
                    self._q_put(("quote", (request_code, q)))
                    if self._quote_fail > 0:
                        self._quote_fail = 0
                        self._report_net(True)
                    self._on_poll_result(True, via_fallback=via_fallback)
            except QuoteDataError as exc:
                # 接口返回数据异常（格式/数值非法）：区别于网络故障，不误判"网络异常"
                # v1.2.95 第3轮走查：与下方网络失败路径一样做「首条 + 每 30 条」
                # 节流。原此处无条件记日志，而本分支就在 0.1s 量级的行情轮询
                # 路径上 —— 接口持续返坏数据时可达 10 条/秒，几小时就把真正
                # 有用的故障记录挤出 5MB×3 代的轮转窗口。
                self._quote_fail += 1
                if self._quote_fail == 1 or self._quote_fail % 30 == 0:
                    log_line("quote data error #%d: %r" % (self._quote_fail, exc),
                             level="ERROR")
                # 改动1：QuoteDataError 同样计入熔断路径，避免坏数据不降频形成请求风暴
                self._on_poll_result(False)
                # v1.4.59（S3 第一处）：坏数据也得上界面那条"数据不新鲜"的标记。
                # 原注释说"区别于网络故障，不误判网络异常"—— 方向对，但代价是
                # **价格冻在旧值而界面一切照常**：呼吸灯正常闪、没有"网络异常"字样，
                # 用户把它当实时价做决定。本项目的降级原则是"保留 + 标滞，不冒充实时"，
                # 所以这里同样按 NET_FAIL_SHOW 阈值上报；措辞沿用现有那条
                # （它回答的是"这个数还能不能用"，而不是"是网线断了还是格式变了"，
                # 后者由上面那行 ERROR 承载）。
                if self._quote_fail >= NET_FAIL_SHOW:
                    self._report_net(False)
            except Exception as exc:
                # 连续失败计数：首条与每 30 次各记一条日志，达阈值上报界面"网络异常"
                self._quote_fail += 1
                self._on_poll_result(False)
                if self._quote_fail == 1 or self._quote_fail % 30 == 0:
                    log_line("quote fetch fail #%d: %r" % (self._quote_fail, exc), level="ERROR")
                if self._quote_fail >= NET_FAIL_SHOW:
                    self._report_net(False)
        finally:
            self._quote_lock.release()

    def _report_net(self, ok):
        """网络状态变化时仅经队列回传主线程；_net_ok 只在主线程 _poll 中更新，
        避免后台线程与 GUI 线程共享可变状态。"""
        self._q_put(("net", ok))

    def _fetch_minute(self):
        """拉取分钟分时。返回值供 _minute_loop 驱动快速重试：
        True=拿到有效点集；False=失败/空返回；None=防重入（上一次请求未完成，跳过判定）。
        v1.2.30：原无返回值，改为显式成败信号，供短期快速重试状态机消费。
        v1.4.7：盘中失败/空返回不再直接清空当日曲线，改投递 minute_stale 由主线程
        判定"保留当日走势+置滞后"还是"跨日/无当日数据才清空"，消除网络抖动闪白屏。
        ⚠ 这里抢的是**防重入守卫** `_minute_busy`，不是数据锁 `_minute_lock`：
        守卫会被攥在整个网络调用期间（实测两源都卡死时 34.3s），而主线程要拿数据锁
        读写 minute_points —— 共用一把就是把 Qt 事件循环押在网络超时上。"""
        if not self._minute_busy.acquire(blocking=False):
            return None
        try:
            code = self.code          # 请求发出时的代码，用于回包校验
            try:
                pts = fetch_minute(code)
                if code != self.code:
                    return None
                if pts:
                    self._q_put(("minute", (code, pts)))
                    # v1.2.71：成功后复位连续失败计数 —— 原实现只在失败时累加、
                    # 从不归零，故障恢复后计数停在 137 之类的历史值，此后任何新的
                    # 故障段都永远凑不到 `== 1`，首条故障日志被永久吞掉（与
                    # _fetch_quote 的 `if self._quote_fail > 0: = 0` 不对称）。
                    if self._minute_fail > 0:
                        self._minute_fail = 0
                    return True
                if self._is_trading_time():
                    raise QuoteDataError("minute returned no valid points during trading")
                return False          # 空返回：无有效点集，视为失败
            except Exception as exc:
                if code != self.code:
                    return None
                self._minute_fail += 1
                if self._minute_fail == 1 or self._minute_fail % 60 == 0:
                    log_line("minute fetch fail #%d: %r" % (self._minute_fail, exc), level="ERROR")
                if self._is_trading_time():
                    # 拉取失败同上：投递滞后信号，由主线程按当日性决定保留或清空。
                    self._q_put(("minute_stale", code))
                return False          # 异常：视为失败
        finally:
            self._minute_busy.release()

    def _apply_minute_points(self, pts):
        """Apply source-dated points; reject yesterday's late reply after the open."""
        try:
            today = _beijing_now().strftime("%Y-%m-%d")
        except Exception:
            today = ""
        day = getattr(pts, "day", today)
        if pts and day and today and day != today and self._is_trading_time():
            self._handle_minute_stale(self.code)
            return
        was_stale = self._minute_stale
        self.minute_points = pts
        if pts:
            # 诊断留痕：最后一次写入有效分时点的墙钟时刻（数据龄的唯一来源）。
            # 表示有效点集接收时刻，不等于最后一笔成交的时间。
            self._minute_last_ok_at = time.time()
            self._minute_day = day
        else:
            self._minute_day = ""
            self._minute_last_ok_at = 0.0
        self._minute_stale = False
        if was_stale:
            self._need_redraw = True
            self._chart_changed = True

    def _handle_minute_stale(self, code):
        """盘中拉取失败/空的收口：切股回包丢弃；当日曲线保留置滞后，跨日才清空。"""
        if code != self.code:
            return                     # 迟到的旧股票失败信号，不影响当前显示
        if not self._is_trading_time():
            if self._minute_stale:
                self._minute_stale = False
                self._need_redraw = True
            return
        try:
            today = _beijing_now().strftime("%Y-%m-%d")
        except Exception:
            today = self._minute_day   # 时钟异常时按"当日"保守处理：保留但标滞后
        if self.minute_points and self._minute_day == today:
            # 已有当日走势：保留曲线，仅标记滞后并显示顶部重试标签。
            if not self._minute_stale:
                self._minute_stale = True
                self._need_redraw = True
            return
        # 无当日数据（空/跨日）：清空避免误导，回到"加载中"，_minute_loop 继续重试。
        if self.minute_points:
            self.minute_points = []
            self._line_ver += 1
            self._chart_changed = True
            self._need_redraw = True
        self._minute_stale = False
        self._minute_day = ""
        self._minute_last_ok_at = 0.0

    # ---------- 冷启动：后台图标就绪后应用 ----------
    def _icons_pending(self):
        """本轮是否真有需要处理的图标/Dock 状态（v1.4.10 功耗）。

        `_apply_icons` 原来挂在 `_poll` 头上无条件执行，实测 60 次/秒 × 20µs
        ≈ 0.15% 单核，而它排的队列整个生命周期只有启动时那一条 app_icon。
        现在先用一次 `empty()` 判断（约 1µs）挡掉，真正干活的次数从
        60/秒 降到个位数。Dock 图标未落地前仍每次检查，保证图标一定出现。
        """
        try:
            return (not self._dock_applied) or (not _ICON_QUEUE.empty())
        except Exception:
            return True

    def _apply_icons(self):
        # Dock 常驻：AppKit 已后台预热，就绪后立即设置 activation policy
        # 兜底：5s 后仍未就绪则主线程同步补一次，保证 Dock 图标一定出现
        if not self._dock_applied:
            if _appkit_ready.is_set() or time.monotonic() - self._start_ts > 5.0:
                self._dock_applied = True
                show_dock()
        icon_arrived = False
        try:
            while True:
                kind, payload = _ICON_QUEUE.get_nowait()
                if payload is None:
                    continue   # 该图标后台生成失败（以 None 占位），跳过并沿用上一版
                if kind == "app_icon":
                    self.app_icon = QIcon(QPixmap.fromImage(payload))
                    icon_arrived = True
                elif kind == "app_icon_dark":
                    self.app_icon_dark = QIcon(QPixmap.fromImage(payload))
                    icon_arrived = True
                else:
                    setattr(self, "_" + kind + "_pm", QPixmap.fromImage(payload))
                self._need_redraw = True
        except queue.Empty:
            pass
        if icon_arrived:
            self._sync_app_icon()

    def _sync_app_icon(self):
        """按当前系统外观挑浅色或深色那张图标，贴到窗口与 Dock。

        深色那张没出好（后台失败）时沿用浅色，不会没有图标。外观没变、图也没变
        就什么都不做。QApplication.setWindowIcon 是 Qt 在 macOS 上换 Dock 图标的入口，
        只设窗口图标不会动 Dock。"""
        icon = self.app_icon_dark if (system_icon_mode() == "dark" and self.app_icon_dark is not None) \
            else self.app_icon
        if icon is None or icon is self._icon_shown:
            return
        self._icon_shown = icon
        self.setWindowIcon(icon)
        QApplication.setWindowIcon(icon)

    def _on_appearance_changed(self, *_scheme):
        """系统浅/深色外观切换的回调（colorSchemeChanged 信号，GUI 线程）。"""
        try:
            self._sync_app_icon()
        except Exception as exc:
            log_swallow("stock_float_window._on_appearance_changed", exc)

    # ---------- 极端场景：窗口初始位置不可见时移回主屏 ----------
    def _clamp_to_screen(self, force=False):
        """窗口被甩出可见区域时移回屏幕内（v1.2.65 第1轮走查修复）。

        原实现用 self._clamped 一次性闸门，且闸门在 try 之前就置位 —— 启动首次
        调用后永久关闭。此后屏幕热插拔/改分辨率时，_on_screen_changed 调本方法
        会立刻 return，窗口被甩到屏幕外再也拉不回来；越界坐标还会被写盘固化，
        重启仍落在屏幕外 —— 即「拔掉外接屏后窗口永久消失」。

        钳制本身是幂等的（窗口已在屏内时不产生位移），故屏幕变化时须无条件执行。
        force=True 用于屏幕变化/热插拔路径；启动路径仍走一次性语义。
        """
        if self._clamped and not force:
            return
        self._clamped = True
        try:
            from PyQt6.QtGui import QGuiApplication
            g = self.frameGeometry()
            scr = QGuiApplication.screenAt(g.center())
            if scr is None:
                scr = QGuiApplication.primaryScreen()
            if scr is None:
                return
            ag = scr.availableGeometry()
            if not ag.intersects(g):
                self.move(ag.x() + (ag.width() - self.width()) // 2,
                          ag.y() + (ag.height() - self.height()) // 3)
                # 立即持久化新坐标：否则越界位置会在下次拖拽/退出时才写盘，
                # 中间若发生崩溃或强退，重启仍落在屏幕外（窗口"消失"）。
                #
                # v1.4.10 功耗：同一坐标只写一次。屏幕热插拔/分辨率重协商会
                # 连续触发 geometryChanged → _clamp_to_screen(force=True)，
                # 窗口在写盘后仍判定越界时（例如可用区刚好等于窗口尺寸）就会
                # 每次事件都做一次"读底稿 + 备份 + 原子写 + fsync"。
                # save_config 本身不是高频路径，唯一的高频入口就是这里，故挡在
                # 写盘前而不是改 save_config 的语义（后者必须保持"调用即落盘"）。
                pos = (self.x(), self.y())
                if pos != getattr(self, "_clamped_saved_pos", None):
                    self._clamped_saved_pos = pos
                    self._after_save_config(
                        save_config(self.code, self.x(), self.y(),
                                    self.height(), self._bg_cfg))
        except Exception:
            pass

    def _watch_screen_geometry(self, screen):
        """监听单个屏幕的可用区域变化（副屏缩放/分辨率调整同样会触发）。"""
        if screen is None:
            return
        try:
            screen.geometryChanged.connect(self._on_screen_changed)
        except Exception:
            pass
        try:
            screen.availableGeometryChanged.connect(self._on_screen_changed)
        except Exception:
            pass

    def _on_screen_added(self, screen):
        """新屏幕加入后补绑定几何变化信号，并按现有规则重新钳制窗口。"""
        self._watch_screen_geometry(screen)
        self._on_screen_changed(screen)

    def _on_screen_changed(self, *args):
        """屏幕热插拔或分辨率变化后，窗口若被甩出可见区域则移回，并重新置顶。

        同时清空玻璃外观缓存；当前玻璃状态会在下一次探测中重新计算。"""
        try:
            self._glass_mode = None
            if not self.isVisible() or self._minimized:
                return
            # v1.2.65：屏幕变化必须强制钳制 —— 不传 force 时 _clamp_to_screen
            # 会被启动期的一次性闸门挡掉，窗口甩出屏幕后拉不回来。
            self._clamp_to_screen(force=True)
            self._keep_on_screen()
            self._force_top()
        except Exception:
            pass

    def _update_nap_policy(self, trading):
        """v1.2.41：App Nap 门控下沉 sf_runtime.NapTokenManager（幂等，状态未变化无系统调用）。
        交易时段 beginActivity 禁止 App Nap，保证行情轮询稳定；否则 endActivity 恢复省电。"""
        return self._runtime.update_nap(trading)

    # ---------- v1.4.10 功耗：交易时段翻转点的精确唤醒 ----------
    def _arm_session_timer(self):
        """把"交易/非交易状态可能翻转"的那一刻武装成一次单发定时器。

        为什么需要：_poll 原来 16ms 一跳，顺带就能发现 09:30 开盘并取 App Nap
        令牌；现在 _poll 退成 1 秒看门狗，最坏会晚 1 秒才禁止 App Nap，而开盘
        那一秒正是行情最密集、最需要满速调度的时刻。这里提前一步精确到位。
        上限 6 小时：跨周末时边界可能很远，QTimer 的 int 毫秒也要防溢出；
        真到点了再重新武装，所以最坏只是一次多余的唤醒，不会漏。
        """
        try:
            secs = seconds_to_session_change()
        except Exception:
            secs = 0.0
        try:
            ms = int(min(max(secs, 0.2), 6 * 60 * 60.0) * 1000)
            self._session_timer.start(ms if ms > 0 else 1)
        except Exception:
            pass

    def _on_session_boundary(self):
        """时段边界到点：复核 App Nap 与呼吸灯时钟，并武装下一个边界。

        呼吸灯在这里也要复核一次（v1.4.15 F2）：时段闸门已从 16ms 热路径移走，
        15:01 收盘那一秒若只靠 `_poll`（盘后 1 秒一跳）停表，会多空转最多 1 秒。
        """
        try:
            trading = self._is_trading_time()
            self._update_nap_policy(trading)
            if not trading and self._minute_stale:
                self._minute_stale = False
                self._need_redraw = True
                self._repaint_now()
        except Exception as exc:
            log_swallow("stock_float_window._on_session_boundary", exc)
        self._sync_dot_timer()
        self._arm_session_timer()

    def _snap_to_edge(self):
        """拖拽松手后边缘吸附：贴着屏幕边缘、或**越过**边缘时，自动贴回边沿。

        v1.4.48 重写判定（用户报「吸附有时不生效，特别是屏幕底部」）：
          · 旧写法是 `abs(卡片边 - 可用区边) <= SNAP_MARGIN`。但拖动路径
            （`mouseMoveEvent` 里裸 `self.move(...)`）**没有钳制**，卡片可以被拖到
            Dock 之下、甚至越出物理屏幕 —— 这时它离**可用区**边反而更远，
            越过物理底边 60px 时距离是 129px（Dock 常驻约 70pt），32pt 的判定带
            根本够不到：越用力贴边越不吸附。顶部菜单栏 24~37pt，时而在带内时而在带外，
            就是「有时候」；左右两侧没有保留区，所以一直正常。
          · 现在改成**带符号距离**：`越过边缘` 记为负数，同样落在判定带内 ——
            越界本身就是最强的贴边意图，一律贴回来。
          · 落点仍然只在**可用区**边沿（不盖 Dock、不被菜单栏吃掉）：本窗口层级是
            NSStatusWindowLevel，压到 Dock 上会把那块 Dock 的点击吞掉，
            那是比"没贴上"更糟的结果。
          · 几何口径统一成**非含端点**（`x + width`）：Qt 的 `QRect.right()/bottom()`
            含端点，旧写法贴齐时恒有 1px 偏差，判定带白占 1px。
        """
        try:
            from PyQt6.QtGui import QGuiApplication
            scr = QGuiApplication.screenAt(self.frameGeometry().center())
            if scr is None:
                scr = QGuiApplication.primaryScreen()
            if scr is None:
                return
            ag = scr.availableGeometry()
            x, y = self.x(), self.y()
            w, h = self.width(), self.height()
            ag_right, ag_bottom = ag.x() + ag.width(), ag.y() + ag.height()

            def _nearest(candidates):
                """在"够得着"的边里挑最近的一条；都够不着就什么都不做。

                距离取带符号值，负数=已越过该边。两个候选用 abs 比远近，
                这样"贴左边 30px"和"越过左边 40px"不会被当成同一档优先级。
                """
                near = [(dist, land) for dist, land in candidates if dist <= SNAP_MARGIN]
                if not near:
                    return None
                return min(near, key=lambda item: abs(item[0]))[1]

            # 顶/底各自只与**可用区边**比（v1.4.59 收掉一条死代码，并改正注释）。
            # 原写法在这里还拿物理边比一次：`min(y - ag.top(), y - sg.top())` ——
            # 因为 sg ⊆ ag 的反向包含（菜单栏让 sg.top() ≤ ag.top()、Dock 让
            # sg.bottom() ≥ ag.bottom()），两个 min 永远取到"可用区"那一项，
            # 物理边那一路从来没生效过，而注释却承诺"用户看的是到物理底的距离"。
            # 真正让"越用力贴边越吸得上"的是**带符号距离**：越过边缘记为负数，
            # 而判定条件是 `dist <= SNAP_MARGIN` —— 任何负数都在带内，
            # 所以拖到 Dock 之下 60px 照样贴回可用区底沿。
            # 落点仍然只在可用区边沿（不盖 Dock、不被菜单栏吃掉）：本窗口层级是
            # NSStatusWindowLevel，压到 Dock 上会把那块 Dock 的点击吞掉。
            # ⚠ 别改成 max：那会让"离底 10px"这种正常贴边变成够不着（10 → 80）。
            nx = _nearest([(x - ag.left(), ag.left()),
                           (ag_right - (x + w), ag_right - w)])
            ny = _nearest([(y - ag.top(), ag.top()),
                           (ag_bottom - (y + h), ag_bottom - h)])
            if nx is None and ny is None:
                return
            nx = x if nx is None else nx
            ny = y if ny is None else ny
            # 落点再钳一次：卡片尺寸接近或超过可用区时（展开分时图 + 小屏/大 Dock），
            # 贴对边会把卡片推到对面的边之外 —— 那是把"没吸上"换成"看不见"。
            nx = max(ag.left(), min(nx, max(ag.left(), ag_right - w)))
            ny = max(ag.top(), min(ny, max(ag.top(), ag_bottom - h)))
            if (nx, ny) != (x, y):
                self.move(nx, ny)
        except Exception as _swallow_exc:
            log_swallow("stock_float_window._snap_to_edge", _swallow_exc)

    def _poll(self):
        # v1.2.48：running 门控 —— 退出流程将 self.running 置 False 后，任何仍可能
        # 触发的 _poll（如 QTimer.singleShot(0) 排队残留、收尾阶段的迟到轮询）直接
        # 返回，杜绝退出后残留的 UI 更新与网络访问。
        if not self.running:
            return
        ticks = getattr(self, "_target_ticks", None)
        if ticks is not None:
            for quote in ticks.take(self.code):
                try:
                    self._evaluate_received_quote(quote)
                except Exception as exc:
                    log_swallow("stock_float_window._poll.target_tick", exc)
        # 缩小到 Dock 期间整体低频；具体间隔由 sf_quote_loop 的决策函数提供。
        # 仍消费行情队列，保证恢复即最新。
        # v1.2.40：间隔决策下沉 sf_quote_loop.ui_poll_interval_ms/ui_top_poll_interval_ms
        if self._minimized:
            iv = ui_poll_interval_ms(True, False)
            if self.timer.interval() != iv:
                self.timer.setInterval(iv)
            tiv = ui_top_poll_interval_ms(True, False)
            if self.top_timer.interval() != tiv:
                self.top_timer.setInterval(tiv)
            self._update_nap_policy(False)
            try:
                if self._icons_pending():
                    self._apply_icons()
                while True:
                    kind, data = self.q.get_nowait()
                    if kind == "quote":
                        data_code, quote = data if (isinstance(data, tuple) and len(data) == 2) else (self.code, data)
                        if data_code != self.code:
                            continue
                        self._apply_quote(quote)
                    elif kind == "minute":
                        # data 形如 (请求时的代码, 分时点列表)：旧股票的迟到回包直接丢弃
                        data_code, data = data if (isinstance(data, tuple) and len(data) == 2) else (self.code, data)
                        if data_code != self.code:
                            continue
                        if data != self.minute_points:
                            self._need_redraw = True
                            self._line_ver += 1
                            # S-1 v1.2.50：与主分支（v1.2.43 第4项）保持一致 ——
                            # 分时数据整表替换也标记图表已变化，恢复展开后图表不错帧
                            self._chart_changed = True
                        self._apply_minute_points(data)
                    elif kind == "minute_stale":
                        self._handle_minute_stale(data)
                    elif kind == "net":
                        if data != self._net_ok:
                            self._net_ok = data
                    elif kind == "update_check":
                        self._handle_update_check(*data)
                    elif kind == "update_check_cancel":
                        self._handle_update_check_cancel(data)
                    elif kind == "update_prompt_answer":
                        self._handle_update_prompt_answer(data)
                    elif kind == "update_progress":
                        self._handle_update_progress(*data)
                    elif kind == "update_download":
                        self._handle_update_download(*data)
            except queue.Empty:
                pass
            except Exception as exc:
                log_line("poll error (minimized): %r" % (exc,))
            return
        # 能耗优化：非交易时段降频（行情本身已降为 60s 低频拉取，
        # UI 无需再 50ms 轮询，避免空转耗电）；交易时段保持 50ms 快速响应
        trading = self._is_trading_time()
        # v1.2.40：间隔决策下沉 sf_quote_loop 纯函数（行为与原字面量逐位一致）
        iv = ui_poll_interval_ms(False, trading)
        if self.timer.interval() != iv:
            self.timer.setInterval(iv)
        tiv = ui_top_poll_interval_ms(False, trading)
        if self.top_timer.interval() != tiv:
            self.top_timer.setInterval(tiv)
        # App Nap 策略随交易状态切换（内部幂等，无状态变化时不产生实际调用）
        self._update_nap_policy(trading)
        # 定时轮询：先合并队列里的同类最新数据，再一次性应用。实时行情只需展示
        # 最新状态，避免积压时重复执行目标价判断、动画重置和图表状态更新。
        # v1.4.10 功耗：见 _icons_pending —— 队列空时不再每拍排一遍空队列
        if self._icons_pending():
            self._apply_icons()
        batch = WindowMessageRouter.drain(self.q, self.code)
        processed = batch.processed
        latest_quote = batch.latest_quote
        latest_minute = batch.latest_minute
        minute_stale_after = batch.minute_stale_after
        latest_net = batch.latest_net
        has_latest_net = batch.has_latest_net
        update_checks = batch.update_checks
        update_check_cancels = batch.update_check_cancels
        update_prompt_answers = batch.update_prompt_answers
        update_progresses = batch.update_progresses
        update_downloads = batch.update_downloads
        if batch.error is not None:
            log_line("poll error: %r" % (batch.error,))

        # v1.4.66 复核（2026-10-12）：这一段此前在兜异常范围**之外** —— 消费队列那段
        # 有 except 兜底，而批末的行情应用与五个 OTA handler 没有。任何一个 handler
        # 抛错（改名/漏 import 的 AttributeError 是真实形态）都会跳过本拍收尾并沿槽上抛，
        # 界面只表现为"点了检查更新没反应"，日志里只有一行 poll error（甚至没有）。
        # 现在整段纳入守卫并留痕；后面的重绘/呼吸灯/宽度收尾无论如何都执行。
        try:
            for quote in batch.quote_evaluations:
                evaluate = getattr(self, "_evaluate_received_quote", None)
                if callable(evaluate):
                    evaluate(quote)
            if latest_quote is not None:
                self._apply_quote(latest_quote)
            if latest_minute is not None:
                if latest_minute != self.minute_points:
                    self._need_redraw = True
                    self._line_ver += 1
                    # 分时数据整表替换后要求下一帧重画图表，避免价格动画期间错帧。
                    self._chart_changed = True
                self._apply_minute_points(latest_minute)
            if minute_stale_after is not None:
                self._handle_minute_stale(minute_stale_after)
            if has_latest_net and latest_net != self._net_ok:
                # 网络状态变化（异常/恢复）：仅状态确实翻转时才更新并重绘
                self._net_ok = latest_net
                self._need_redraw = True
        except Exception as exc:
            # 留痕理由（AGENTS §6 第 9 条）：这里吞掉的可能是"行情应用写错名字"，
            # 静默等于"行情停更"且查无可查。log_line 已限流，热路径安全。
            log_line("poll apply error: %r" % (exc,), "WARNING")
        # 取消要**先于**结果应用：同批里既有「取消」又有回包时，用户的意思是不再等，
        # 让结果先跑就会弹一个他刚按掉的那一类框。
        # 五类 OTA 消息逐条兜：一类 handler 写错不该把其余四类的消息一起带走
        # （比如 update_check 炸了，下载进度还得继续走）。
        for button in update_check_cancels:
            try:
                self._handle_update_check_cancel(button)
            except Exception as exc:
                log_line("poll apply error: %r" % (exc,), "WARNING")
        for update_data in update_checks:
            try:
                self._handle_update_check(*update_data)
            except Exception as exc:
                log_line("poll apply error: %r" % (exc,), "WARNING")
        for update_data in update_prompt_answers:
            try:
                self._handle_update_prompt_answer(update_data)
            except Exception as exc:
                log_line("poll apply error: %r" % (exc,), "WARNING")
        for update_data in update_progresses:
            try:
                self._handle_update_progress(*update_data)
            except Exception as exc:
                log_line("poll apply error: %r" % (exc,), "WARNING")
        for update_data in update_downloads:
            try:
                self._handle_update_download(*update_data)
            except Exception as exc:
                log_line("poll apply error: %r" % (exc,), "WARNING")
        if processed and not trading:
            # 非交易时段的 UI 轮询间隔由定时器决策提供；零延迟消化保证首条行情立即上屏。
            # 交易时段同理由既有定时器处理，避免每条消息额外投递一次空轮询。
            QTimer.singleShot(0, self._poll)
        # 数据未变化时不重绘：静止状态零绘制开销（动画/悬停/拖动各自驱动重绘）
        if self._need_redraw:
            self._need_redraw = False
            # v1.4.15 F1：这里改成统一入口，是 v1.4.12 那个想法的**正确版本**。
            # v1.4.12 无条件砍掉这次整窗重绘 → 呼吸灯从 10 帧/秒 冻到 0.6。
            # 现在只在"呼吸灯主钟正在跑"时跳过 —— 那时它 ≤16ms 内就会请求一次
            # 整窗帧，这次重绘确实是重复排队；主钟没跑（收盘后/图表收起/滞后冻结/
            # 缩 Dock/不可见）时照旧立即重绘，v1.4.13 的护栏语义一字未退。
            self._repaint_now()
        # v1.4.14：呼吸灯时钟的武装状态在这里统一复核 —— 分时数据到达、滞后态
        # 翻转、图表收展完成等都会改变"此刻有没有可见动画"，靠这一处兜住即可；
        # 缩 Dock / 恢复窗口另外显式调用，不等下一拍。
        self._sync_dot_timer()
        # 固定宽度兼容路径：底部文本变化仍更新测量状态，但窗口宽度保持 W。
        if self._width_dirty:
            self._width_dirty = False
            self._auto_width()

    # ---------- 固定宽度兼容路径 ----------
    def _bottom_texts(self):
        """返回底部双行文本，供绘制与兼容测量路径共用。

        返回 (row1, row2)，每项为 (标签, 数字, 颜色)：
          row1：上行 —— 最大涨幅（相对昨收，下限零）+ 最高点
          row2：下行 —— 最大跌幅（相对昨收，上限零）+ 最低点
          幅度为零时显示灰色 0.00%；昨收未知显示 "--"，不回退今开。
          高低价格独立按 _fmt_hl 归一，非法显示 "--"，不受幅度截零影响。

        v1.2.71 第4轮走查：文本构造下沉为 sf_ui_render.bottom_rows **唯一实现**。
        此前本方法与 _paint_bottom 各写一份（逐字复制），属典型双份漂移 ——
        一处是「窗口最小宽度按之计算的文本」，另一处是「实际画出来的文本」，
        改一处漏一处就会让两者不一致，表现为窗口宽度算窄、底部最后一段被
        静默裁掉。现收敛为唯一来源，本方法仅保留薄委托。
        """
        return bottom_rows(self.quote)

    def _update_width_dirty(self):
        """记录底部文本变化，供固定宽度兼容路径保持内部状态一致。"""
        try:
            rows = self._bottom_texts()
            key = tuple(
                (label, number)
                for row in (rows or ())
                for label, number, _color in row
            )
        except Exception:
            key = None
        # 部分单元测试用未初始化的 FloatWindow 调用 _apply_quote；通过 __dict__
        # 读取避免 PyQt 父类尚未初始化时的属性访问异常。
        if key != self.__dict__.get("_width_content_key"):
            self._width_content_key = key
            self._width_dirty = True

    def _content_min_width(self):
        """主界面宽度恒为 sf_constants.W = 184px（v1.4.60 复核 F4 把这句注释改正）。

        原先写「固定使用 160px 窄版宽度」——那是更早一版的说法，与 `return W` 和
        `W, H = 184, 192` 矛盾了十几个版本；v1.4.58 定宽代价那句「只有 160px 极限窄版
        才降档」正是从这句过时注释抄出去的，而 160px 在当前代码里根本不可达
        （`setFixedWidth(W)` 把窗口锁死在 184）。名称按可用宽度省略、价格字号自动收缩、
        极端长价格按既有优先级隐藏次要徽标，这些行为不变，只是把宽度说对。
        """
        return W

    def _auto_width(self):
        """按内容计算所需宽度并应用；宽度变化 ≥3px 才调整，避免抖动。
        v1.2.34：切股过渡期（quote 为 None）保持当前宽度，直接 return——
        否则 _content_min_width 在无 quote 时退回最小宽 base(W)，会把窗口
        临时收窄到最小宽，行情到达后再放宽，右侧边缘"先收后放"闪动。
        行情到达（_apply_quote 写入 quote）后由 _poll 每轮调用正常自适应。"""
        if not self.quote:
            return
        try:
            nw = self._content_min_width()
            cur = self.width()
            if abs(nw - cur) >= 3:
                self.setFixedWidth(nw)
                self._keep_on_screen()
        except Exception as exc:
            log_line("autowidth error: %r" % (exc,))

    def _keep_on_screen(self):
        """窗口任一边缘越出屏幕可用区时回移，保持完整可见。

        v1.2.65 第1轮走查修复：原实现只修右边缘与上边缘（为「窗口变宽」场景设计），
        不修左/下边缘。拔掉外接屏或分辨率变小时，窗口可能整体落在新屏幕的**下方**
        之外，此时本方法毫无作用 —— 与 _clamp_to_screen 的一次性闸门叠加，窗口就
        彻底消失在屏幕外且重启也回不来。现补全四个方向。
        """
        try:
            from PyQt6.QtGui import QGuiApplication
            scr = QGuiApplication.screenAt(self.frameGeometry().center()) or QGuiApplication.primaryScreen()
            if scr is None:
                return
            ag = scr.availableGeometry()
            fg = self.frameGeometry()
            x, y = self.x(), self.y()
            # v1.4.59（S2 第三处）：几何口径统一成**非含端点**，与 _snap_to_edge 同一套。
            # Qt 的 QRect.right()/bottom() 含端点，旧写法在这里把"已经贴齐"的卡片
            # 又往左挪 1px —— 而 v1.4.48 给用户的承诺是"贴齐不许挪 1 像素"，
            # 于是吸附与回移两个函数互相推翻（下一次内容变宽/_keep_on_screen 被调即现形）。
            # 改成 exclusive 之后，贴齐时算出来的落点与当前位置相同，下面的守卫直接不动。
            ag_right, ag_bottom = ag.x() + ag.width(), ag.y() + ag.height()
            x, y = keep_on_screen_position(
                x, y, fg.width(), fg.height(), ag.left(), ag.top(),
                ag_right, ag_bottom, padding=4)
            if (x, y) != (self.x(), self.y()):
                self.move(x, y)
        except Exception:
            pass

    # ---------- 数字跳动动效 ----------
    def _quote_zero_fallback(self, data, price, pct):
        """v1.2.28：0 值兜底 —— 非交易时段（收盘/休市/盘前）或接口偶发失败/空返回。

        触发条件：展示价 <= 0，或当前处于非交易时段（sf_quote.market_status() 非 None）。
        兜底源：分时缓存 self.minute_points 的最后一点价格（当日最后成交价/收盘价），
        涨跌幅 = (兜底价 - 昨收) / 昨收 * 100 重算；昨收缺失/非法时仅兜底价格不动涨跌幅。
        回写 data 的 price/pct（self.quote 字段同源）以驱动动画与展示。
        没有任何分时数据可兜底 → 维持原样（-- 或 0），不编造数据。
        """
        if price > 0 and market_status() is None:
            return price, pct
        if not getattr(self, "minute_points", None):
            return price, pct
        try:
            last_price = float(self.minute_points[-1][1])
        except (TypeError, ValueError, IndexError):
            return price, pct
        if last_price <= 0 or not math.isfinite(last_price):
            return price, pct
        new_pct = pct
        try:
            pre_close = float(data.get("pre_close"))
        except (TypeError, ValueError):
            pre_close = 0.0
        if pre_close > 0 and math.isfinite(pre_close):
            new_pct = (last_price - pre_close) / pre_close * 100.0
        # 与 _apply_quote 的 _anim_decimals 取位规则一致，保持原有小数位
        # S-2 v1.2.50：复用 S8 的纯数字/长度校验 —— 形如 "123."（split 得空串）
        # 或 "12.a"（非纯数字）时 dec=0 会导致兜底价丢小数位，一律回落 2 位
        s = data.get("price", "")
        _frac = s.split(".", 1)[1] if isinstance(s, str) and "." in s else ""
        dec = len(_frac) if (_frac and _frac.isdigit() and len(_frac) <= 4) else 2
        data["price"] = ("%%.%df" % dec) % last_price
        data["pct"] = str(round(new_pct, 2))
        return last_price, new_pct

    def _resolve_pct(self, data, price):
        """v1.3.49：宽容解析涨跌幅，返回 (pct, unknown)。

        主源 sf_quote._fetch_quote_primary 的 pct 是**尾部扩展字段**容错读取的
        （"pct": _num(32)），字段缺失时 _num 返回空串 ""；旧逻辑 float("") 抛
        ValueError → 整条行情被 return 丢弃，现价明明是好的却一直显示上一次的
        值甚至 "--"，用户以为软件卡死。价格是核心信息，涨跌幅只是辅助信息，
        缺涨跌幅不该连价格一起丢。

        解析顺序：
          1. data["pct"] 能转出有限数 → 用它（unknown=False）；
          2. 否则用 data["pre_close"] 自算 (price - pre) / pre * 100，
             仅当 pre 是有限数且 pre > 0；
          3. 都算不出来 → (0.0, True)。

        返回值 pct **恒为数值**：new_pct 会参与动画算术（quote_changed /
        abs(new_pct - self._disp_pct) / _anim_from_pct/_anim_to_pct），
        传 None 会直接抛 TypeError，故未知时回落 0.0，语义由 unknown 承载。
        """
        try:
            v = float(data.get("pct"))
            if math.isfinite(v):
                return v, False
        except (TypeError, ValueError, AttributeError):
            pass
        try:
            pre = float(data.get("pre_close"))
            if math.isfinite(pre) and pre > 0:
                v = (price - pre) / pre * 100.0
                if math.isfinite(v):
                    return v, False
        except (TypeError, ValueError, AttributeError):
            pass
        return 0.0, True

    def _apply_quote(self, data):
        """收到新行情：价格/涨跌幅从当前显示值平滑滚动到新值"""
        try:
            new_price = float(data["price"])
            if not math.isfinite(new_price):
                return
        except (TypeError, ValueError, KeyError):
            # 价格缺失/非法（停牌/接口返回空值等）时丢弃本条，避免崩溃
            # v1.3.49：只有**价格**仍严格 —— 无价可显示，沿用旧行为丢弃整条。
            return
        # v1.3.49：涨跌幅改为宽容解析（详见 _resolve_pct 注释）。此处只取初值供
        # _quote_zero_fallback 使用，真正提交 _pct_unknown 在零价兜底之后。
        new_pct, _pct_unknown = self._resolve_pct(data, new_price)
        # v1.2.28：价格/涨跌幅 0 值兜底 —— 腾讯接口在非交易时段 price 返回 0，
        # 接口偶发失败/空返回也会落到 0。用已缓存分时数据最后一点价格（当日最后
        # 成交价/收盘价）作为展示价，涨跌幅按 (价-昨收)/昨收*100 重算，并同步
        # 回写 data（即 self.quote 的 price/pct）与动画目标值，动画逻辑不受影响。
        # R48 第 4 轮走查：区分「真实成交价」与「非交易时段兜底价」。
        # 腾讯接口在盘前/休市返回 0，_quote_zero_fallback 会用分时缓存最后一点
        # （盘前仍是**上一交易日**的收盘价）填充显示值并回写 quote["price"]。
        # 兜底价存在的意义只是让界面不显示 "--"，它不是今天的成交价 ——
        # 拿它判定会让「跌到 X」在开盘前就被昨收误触发。
        self._real_price = new_price if quote_is_current(data) else 0.0
        self._price_is_fallback = not (new_price > 0 and market_status() is None
                                       and quote_is_current(data))
        try:
            new_price, new_pct = self._quote_zero_fallback(data, new_price, new_pct)
        except Exception as _swallow_exc:
            log_swallow("stock_float_window._apply_quote", _swallow_exc)
        # v1.2.30：无有效行情一律显示 "--" —— 价格为 0/非法且无分时数据可兜底时，
        # 不更新 _disp_price/_disp_pct（保持 None 或上一有效值），渲染层 _paint_price
        # 无数据分支输出 "--"，绝不显示 0/0.00/0%。首次见到时仍记录 self.quote 以支持
        # 停牌徽标识别；已有有效行情时保持原显示不被 0 覆盖；有分时兜底不受影响。
        if new_price <= 0:
            if self.quote is None:
                self.quote = data
                self._update_width_dirty()
            now = _beijing_now()
            self._last_ok_time = "%02d:%02d:%02d" % (now.hour, now.minute, now.second)
            self._need_redraw = True
            return
        # v1.3.49：兜底与丢弃分支**之后**才提交 _pct_unknown ——
        #   ① _quote_zero_fallback 会把自算涨跌幅回写 data["pct"]，重算才能反映它；
        #   ② 被丢弃的行情（无价可显示）不污染未知态，避免永久卡在 "--"；
        #   ③ 涨跌幅重新可用时必须置回 False（同一条行情后续又正常了要恢复显示）。
        new_pct, self._pct_unknown = self._resolve_pct(data, new_price)
        # 2.0 v1.2.0：行情应用成功即刷新最近成功时间（断网时名称栏展示）
        # R47：统一用北京时间，不再用电脑本地时区（换时区会显示错误时刻）。
        now = _beijing_now()
        if quote_is_current(data, now):
            self._last_ok_time = "%02d:%02d:%02d" % (now.hour, now.minute, now.second)
        # 切换股票瞬间，旧股票的排队行情到达会被丢弃，避免覆盖新股票显示
        # v1.2.48(B2)：比较前先做代码归一（去前缀、北交所补 0 到 6 位）——
        # 自选代码 "bj8307" 与行情回包补 0 后的 "830700" 直接比较不等，会被误判
        # 为旧股票迟到回包而静默丢弃、价格不更新。
        c6 = data.get("code")
        if isinstance(c6, str):
            norm_cur = _normalize_code_6(self.code)
            norm_c6 = _normalize_code_6(c6)
            if norm_cur and norm_c6 and norm_cur != norm_c6:
                return
        # 12.x v1.2.11：实时行情回调中判断跌幅阈值穿越，触发下跌闪烁。
        # 放在所有分支之前，确保微小变动/动画分支同样会被评估；且位于
        # 切股排队行情丢弃判断之后，避免旧股票行情在切换瞬间误触发。
        self._maybe_trigger_drop_flash(new_pct)
        # v6：「目标价」实时判定（置于切股丢弃判定之后，仿 _maybe_trigger_drop_flash 位置）。
        # 命中即通知（语义键去重）+ 彩虹高亮 + 持久化已 hit 状态；不重复触发。
        self._evaluate_received_quote(data)
        # 铃铛气泡正开着时让它跟上最新现价与触发状态（气泡没开时这里几乎零开销）。
        # 紧跟判定之后：刚触发的那一条要立刻从「待触发」挪到「已触发」。
        refresh_bubble = getattr(self, "_refresh_target_bubble", None)
        if refresh_bubble is not None:
            try:
                refresh_bubble()
            except Exception as _swallow_exc:
                log_swallow("stock_float_window._apply_quote.bubble_refresh", _swallow_exc)
        s = data["price"]
        # v1.2.48(S8)：小数位解析增加长度校验 —— 形如 "123."（split 得空串）时
        # decimals=0 会导致展示精度异常；非纯数字/超长异常值一律回落 2 位。
        _frac = s.split(".", 1)[1] if "." in s else ""
        self._anim_decimals = len(_frac) if (_frac and _frac.isdigit() and len(_frac) <= 4) else 2
        # 高/低变化检测：价格未变但创日内新高/新低时，底部高/低行也要刷新
        old_hl = None
        if self.quote:
            try:
                old_hl = (self.quote["high"], self.quote["low"])
            except Exception:
                old_hl = None
        new_hl = (data["high"], data["low"])
        hl_changed = old_hl is not None and old_hl != new_hl
        self.quote = data
        self._update_width_dirty()
        # v1.3.45：顺手刷新首屏快照（内部按 LAST_QUOTE_MIN_WRITE_INTERVAL 节流，
        # 8 次/秒的行情不会变成 8 次/秒的写盘）
        try:
            self._maybe_cache_first_screen()
        except Exception as _swallow_exc:
            log_swallow("stock_float_window._apply_quote", _swallow_exc)
        if self._disp_price is None:
            # 首次/切换股票：直接显示，不动画
            self._disp_price = new_price
            self._disp_pct = new_pct
            self._anim_timer.stop()
            # v1.2.52：防御性清除滚动目标残留（正常路径 apply_stock 已清，
            # 此处兑底任何绕过 apply_stock 的首帧路径），防渲染层锁旧目标
            # v1.2.55：同步清除 _roll_prev/_roll_anim，避免首帧时残留的旧
            # 滚动基线/条目干扰新股票的首次渲染
            self._roll_pending = {}
            self._roll_prev = {}
            self._roll_anim = {}
            # v1.2.38：同步价格滚动基线（from=to=当前值），避免 __init__ 残留的
            # _anim_* =0 使后续布局动画复用 _anim_timer 时把 _disp_price 误覆盖为 0
            self._anim_from_price = new_price
            self._anim_to_price = new_price
            self._anim_from_pct = new_pct
            self._anim_to_pct = new_pct
            self._need_redraw = True
        elif quote_changed(new_price, new_pct, self._disp_price, self._disp_pct):
            # 跌破重要整数关卡（50 的整数倍：50/100/150/200/250...）时价格闪烁一次。
            # 例：404→399.99 破 400 触发；410→409.99 破 410（非关键关卡）不触发。
            # 基准取上一次动画的**目标价**而非当前显示值：快速连跳时用插值中间值会漏判跨关卡
            old_price = self._anim_to_price if self._anim_to_price > 0 else self._disp_price
            if new_price < old_price and self._crossed_key_level(old_price, new_price):
                self._start_price_flash()
            # 动画参数调整：微小变动直接赋值不动画；时长与阈值由 sf_constants 的
            # ANIM_* 常量决定，避免在这里复制会漂移的数值。
            # 第1轮走查修复：开关名纠正 anim_tune → anim_params —— anim_tune 不在
            # _FEATURE_DEFAULTS，feature_enabled 恒返回 False，导致微小变动直赋/130ms
            # 常规/200ms 大幅的动画调优（v1.2.5）从未生效，一直回退固定 0.2s。
            if feature_enabled("anim_params"):
                delta_pct = abs(new_pct - self._disp_pct)
                delta_price = abs(new_price - self._disp_price)
                # v1.2.40：微小变动判定/动画时长决策下沉 sf_animator 纯函数
                if is_tiny_move(delta_price, delta_pct, ANIM_TINY_PRICE) \
                        or self._animations_suspended():
                    self._disp_price = new_price
                    self._disp_pct = new_pct
                    self._anim_timer.stop()
                    # v1.2.52 修复「滚动卡中途」：微小变动直赋会打断
                    # 进行中的插值动画（停表），旧逻辑不清 _roll_pending →
                    # 渲染层永久锁定旧目标串，价格冻结。
                    # v1.2.55 追加修复（真正病根）：停表直赋后 _disp_price
                    # 已跳到新值，但渲染层 _roll_prev（"上一帧显示串"）还停在
                    # 旧目标串 → 下一帧对比发现"有变化"→ 创建从旧目标到新值
                    # 的滚动条目 → 再下一帧 _roll_prev 被更新为新值 → 滚动
                    # 条目因无新触发而永远跑不完 → 画面卡在中途。
                    # 同步 _roll_prev 使渲染层下一帧看到"无变化"，不创建滚动。
                    self._roll_pending = {}
                    self._roll_prev = {}
                    self._roll_anim = {}
                    self._partial_price = None
                    self._need_redraw = True
                    return
                self._anim_dur = anim_duration_ms(delta_pct, ANIM_LARGE_PCT, ANIM_LARGE_MS, ANIM_NORMAL_MS) / 1000.0
            else:
                self._anim_dur = 0.2
            # 数值变化：从当前显示值开始滚动到新值
            self._anim_from_price = self._disp_price
            self._anim_from_pct = self._disp_pct
            self._anim_to_price = new_price
            self._anim_to_pct = new_pct
            self._anim_start = time.monotonic()
            self._anim_timer.start()
            # v1.2.25：预置逐位滚动的最终目标串（price/pct），render 层据此
            # 从旧显示值一次滚到最终值，插值中间帧不再重启滚动动画（修复乱跳）
            try:
                from sf_utils import fmt_pct
                self._roll_pending = {
                    "price": ("%%.%df" % self._anim_decimals) % new_price,
                    "pct": fmt_pct(new_pct),
                }
            except Exception:
                self._roll_pending = {}
            # v1.4.13 回退 v1.4.12：这里**必须**继续无条件请求整窗重绘，不能只靠动画帧。
            #
            # v1.4.12 曾判断"动画定时器 ≤16ms 内会重绘价格条带，这一帧是纯重复"，
            # 省掉后盘中绘制从 68 帧/秒降到 56、真实窗口 CPU 16.85% → 13.80%。
            # 价格数字确实还在滚，但**分时图末端的呼吸灯被冻住了**：
            #   · 呼吸相位按墙上时钟算（sf_ui_render._draw_chart_dot：
            #     time.monotonic() % CHART_DOT_BREATH_PERIOD），只有真正重画图表的
            #     那一帧才会更新；
            #   · 而价格动画的局部脏区按设计"绝不侵入分时图区"（price_update_rect
            #     底部截断到 CHART_TOP），动画帧一帧都不会重画图表。
            # 于是整窗重绘从"每条行情一次"(≈10 次/秒) 掉到只剩分时数据到达那一次
            # (≈1 次/秒)，呼吸灯从平滑起伏变成约 1 秒一跳。老板明确要求不得牺牲高帧率，
            # 因此恢复整窗重绘。
            #
            # 真要拿回这 3 个百分点的电，正确做法不是"少画几帧"，而是让整窗重绘变便宜：
            # 分时图内容 1 秒才变一次，却被重画 10 次/秒 —— 应当把图表区缓存成 QPixmap
            # （键：几何 + _line_ver + _minute_stale），整窗帧改成"贴缓存 + 画呼吸点"，
            # 帧率一位不减，开销照样降下来。
            self._need_redraw = True
        elif hl_changed:
            self._need_redraw = True

    # ---------- v1.3.45：首屏数据快照（冷启动预填） ----------
    #
    # 为什么需要：冷启动时行情线程刚起步，首帧必然还没有数据 —— 界面先画一版
    # 占位符（价格 "--"、分时图空白），等第一条真实行情到了再整屏换掉。用户看到
    # 的就是"窗口先出来、内容再跳一下"，也就是反馈里的"冷启动变形"。
    # 现在把最后一条行情落盘，启动时先填进显示状态：第 1 帧就是有价格的成品，
    # 真实行情到达后只做一次小幅滚动，不再有整屏内容替换。
    #
    # 刻意**不复用 _apply_quote**：那条链路会评估目标价、触发下跌闪烁、记最近
    # 成功时间。缓存价不是今天的成交价，拿它判定会在开盘前就把「跌到 X」误触发。
    # 这里只填显示状态，并把判定价（_real_price）置 0、_price_is_fallback 置真，
    # 保证任何业务逻辑都读不到这个缓存价。
    def _seed_first_screen(self):
        """用 last_quote.json 预填首屏；无快照/代码不符/数据非法时静默跳过。

        SF_SEED_FIRST_SCREEN=0 可关闭（测试用）：同一进程里 A 用例写快照、
        B 用例构造主窗时被预填，会让「无数据显示 --」这类断言全部串味。
        """
        if os.environ.get("SF_SEED_FIRST_SCREEN", "1") == "0":
            return
        try:
            snap = load_last_quote()
        except Exception:
            return
        if not isinstance(snap, dict):
            return
        try:
            cached_code = str(snap.get("code") or "").strip()
            if not cached_code or _normalize_code_6(cached_code) != _normalize_code_6(self.code):
                return                      # 换过股票：缓存价是别家的，不能用
            q = snap.get("quote")
            if not isinstance(q, dict):
                return
            price = float(q.get("price") or 0)
            pct = float(q.get("pct") or 0)
            if not (math.isfinite(price) and math.isfinite(pct)) or price <= 0:
                return
        except (TypeError, ValueError, KeyError):
            return
        # 涨跌幅只在「同一天」才复用：百分比属于某个交易日，隔夜的涨跌幅拿到
        # 今天显示会误导。价格/名称/高低价是"最后已知事实"，可以一直显示。
        try:
            today = _beijing_now().strftime("%Y-%m-%d")
        except Exception:
            today = ""
        pct = pct if (today and snap.get("day") == today) else 0.0
        self.quote = q
        self._disp_price = price
        self._disp_pct = pct
        # v1.3.49：快照是与 _apply_quote 并列的另一条赋值路径，同样要清未知态，
        # 否则上一只股票的 "--" 会残留到这只股票上。
        self._pct_unknown = False
        self._real_price = 0.0          # 判定价置 0：缓存价一律不参与目标价/闪烁判定
        self._price_is_fallback = True
        self._anim_from_price = price
        self._anim_to_price = price
        self._anim_from_pct = pct
        self._anim_to_pct = pct
        self._roll_pending = {}
        self._roll_prev = {}
        self._roll_anim = {}
        self._partial_price = None
        pts = snap.get("minute")
        minute_day = str(snap.get("minute_day", snap.get("day")) or "")
        if isinstance(pts, list) and pts and (not self._is_trading_time() or minute_day == today):
            try:
                with self._minute_lock:
                    self.minute_points = [tuple(p) for p in pts
                                          if isinstance(p, (list, tuple)) and len(p) >= 2]
                    # v1.3.51：首屏快照写入也必须失效折线/均价线缓存，否则切股后可能残留上一只股票的图
                    self._line_ver += 1
                # v1.4.7：快照自带交易日 —— 记录归属日，盘中首次拉取失败时
                # 据此区分"保留当日曲线"还是"清空跨日曲线"。
                self._minute_day = minute_day
                self._minute_stale = bool(self.minute_points and self._is_trading_time())
            except Exception:
                pass
        self._update_width_dirty()
        self._need_redraw = True
        self._seeded_from_cache = True

    def _maybe_cache_first_screen(self):
        """节流写首屏快照：最多每 LAST_QUOTE_MIN_WRITE_INTERVAL 秒落一次盘。"""
        now = time.monotonic()
        last = getattr(self, "_last_quote_saved_at", 0.0)
        if last and (now - last) < LAST_QUOTE_MIN_WRITE_INTERVAL:
            return
        self._last_quote_saved_at = now
        try:
            day = _beijing_now().strftime("%Y-%m-%d")
        except Exception:
            day = ""
        try:
            with self._minute_lock:
                pts = MinutePoints(self.minute_points, day=self._minute_day)
        except Exception:
            pts = MinutePoints()
        self._snapshot_writer.submit(self.code, self.quote, pts, day)

    # ---- 动画驱动状态委托（v1.2.48 下沉 sf_anim_driver.AnimDriver）----
    # property 保持旧字段名（_anim_*/_price_flash_*/_flash_drop_*/_layout_anim_*），
    # 主窗既有读写点零改动，实际读写转发到 self._anim_driver 对应字段。
    _anim_from_price = property(
        lambda self: self._anim_driver.anim_from_price,
        lambda self, v: setattr(self._anim_driver, "anim_from_price", v))
    _anim_to_price = property(
        lambda self: self._anim_driver.anim_to_price,
        lambda self, v: setattr(self._anim_driver, "anim_to_price", v))
    _anim_from_pct = property(
        lambda self: self._anim_driver.anim_from_pct,
        lambda self, v: setattr(self._anim_driver, "anim_from_pct", v))
    _anim_to_pct = property(
        lambda self: self._anim_driver.anim_to_pct,
        lambda self, v: setattr(self._anim_driver, "anim_to_pct", v))
    _anim_start = property(
        lambda self: self._anim_driver.anim_start,
        lambda self, v: setattr(self._anim_driver, "anim_start", v))
    _anim_dur = property(
        lambda self: self._anim_driver.anim_dur,
        lambda self, v: setattr(self._anim_driver, "anim_dur", v))
    _anim_decimals = property(
        lambda self: self._anim_driver.anim_decimals,
        lambda self, v: setattr(self._anim_driver, "anim_decimals", v))
    _price_flash_until = property(
        lambda self: self._anim_driver.price_flash_until,
        lambda self, v: setattr(self._anim_driver, "price_flash_until", v))
    _price_flash_on = property(
        lambda self: self._anim_driver.price_flash_on,
        lambda self, v: setattr(self._anim_driver, "price_flash_on", v))
    _flash_drop_last = property(
        lambda self: self._anim_driver.flash_drop_last,
        lambda self, v: setattr(self._anim_driver, "flash_drop_last", v))
    _flash_drop_on = property(
        lambda self: self._anim_driver.flash_drop_on,
        lambda self, v: setattr(self._anim_driver, "flash_drop_on", v))
    _flash_drop_count = property(
        lambda self: self._anim_driver.flash_drop_count,
        lambda self, v: setattr(self._anim_driver, "flash_drop_count", v))
    _layout_anim_from = property(
        lambda self: self._anim_driver.layout_anim_from,
        lambda self, v: setattr(self._anim_driver, "layout_anim_from", v))
    _layout_anim_to = property(
        lambda self: self._anim_driver.layout_anim_to,
        lambda self, v: setattr(self._anim_driver, "layout_anim_to", v))
    _layout_anim_start = property(
        lambda self: self._anim_driver.layout_anim_start,
        lambda self, v: setattr(self._anim_driver, "layout_anim_start", v))

    def _anim_tick(self):
        # 定时槽：整体 try 包裹，动画异常不影响主循环
        try:
            self._anim_tick_inner()
        except Exception as exc:
            log_line("anim error: %r" % (exc,))

    def _has_active_roll_animation(self):
        """逐位滚动仍有待绘制帧时保持主动画时钟运行。

        价格插值先于渲染层结束：完成帧会把数值落定并清 pending，但
        _draw_roll_text 还需要后续几帧才能把旧字滑出、新字滑入完成。
        """
        return any(bool(entries) for entries in self._roll_anim.values())

    def _anim_tick_inner(self):
        """v1.2.48：帧决策下沉 sf_anim_driver.AnimDriver.tick —— driver 依据动画
        驱动状态输出「动作列表」，本方法仅按序执行 UI 副作用
        （resize / update / 停表 / min-max 收口 / 玻璃 mask 对齐 / 局部重绘）。

        帧逻辑与拆分前逐位一致，关键语义（版本注释随代码迁移至 driver.tick）：
        - 拉伸帧合并（v1.2.28）：_resizing 期间稳定帧率单次应用目标高度；
        - 布局动画与数值动画同帧推进，布局动画进行中直接 return（v1.2.9）；
        - 价格滚动 guard：from==to 且 pct 相等直接跳过、绝不覆盖 _disp_price（v1.2.38）；
        - 中间帧按 should_draw_chart 决策局部条带重绘 / 整窗重绘（v1.2.43）。"""
        actions = self._anim_driver.tick(
            getattr(self, "_resizing", False), self._resize_target_h,
            self.height(), self.width(),
            getattr(self, "_chart_expanded", False), self._chart_changed,
            time.monotonic(), self._disp_price, self._disp_pct,
            LAYOUT_ANIM_DUR)
        if not actions:
            # v1.2.65 第1轮走查修复（16ms 定时器永久空转）：本帧无事可做
            # （无拉伸目标 / 无进行中布局动画 / 价格无滚动）时原实现不停表。
            # v1.3.69：价格插值可能先结束，而渲染层仍有逐位滚动条目；此时
            # 必须保留时钟并请求后续重绘，让旧字/新字完成滑动，避免卡在半途。
            if self._has_active_roll_animation():
                # v1.4.15 F1：走统一入口，呼吸灯主钟在跑时不再重复排一帧
                self._repaint_now()
                return
            # v1.4.28 跟手性：拉伸手势进行中**不停表**。
            # 原来高度一追上目标就停表，下一个鼠标事件再 start() 又要等满 16ms
            # 才应用 —— 实测拉伸 24 拍里只应用了 13 次（`resizeEvent` 11 次独立印证），
            # 拖尾均值 2.96px、最差 6px。保持时钟在跑，应用节奏不再随事件停启；
            # 改后拖尾均值 1.57px（最差仍 6px，落在单帧量化内）。保持窗口由
            # mouseMoveEvent 逐拍续期，松手或超时立刻照常停表，不会退化成 v1.2.65
            # 修掉的"16ms 定时器永久空转"。
            if (getattr(self, "_resizing", False)
                    and time.monotonic() < getattr(self, "_resize_hold_until", 0.0)):
                return
            try:
                self._anim_timer.stop()
            except Exception as _swallow_exc:
                log_swallow("stock_float_window._anim_tick_inner", _swallow_exc)
            return
        for act in actions:
            kind = act[0]
            payload = act[1] if len(act) > 1 else None
            if kind == "resize":
                try:
                    self.resize(payload[0], payload[1])
                except Exception as _swallow_exc:
                    log_swallow("stock_float_window._anim_tick_inner", _swallow_exc)
            elif kind == "update":
                self._repaint_now()
            elif kind == "update_rect":
                self._repaint_now(*payload)
            elif kind == "price":
                self._disp_price, self._disp_pct = payload
            elif kind == "price_finish":
                # v1.2.72：价格与收展可同帧推进；若收展仍未结束，不能因
                # 价格先结束就停掉共用定时器，否则窗口会卡在半高。
                layout_still_running = (any(a[0] == "resize" for a in actions)
                                        and not any(a[0] == "layout_minmax" for a in actions))
                if not layout_still_running and not self._has_active_roll_animation():
                    self._anim_timer.stop()
                self._roll_pending = {}   # v1.2.25：插值结束，逐位滚动目标已落定
                # v1.2.43：完成帧走整窗重绘 —— 图表（分时/折线缓存）+ 价格一起以最终
                # 帧落地，图表与价格同帧对齐不错帧；局部脏区清空恢复整窗路径。
                self._partial_price = None
                self._repaint_now()
            elif kind == "price_mid":
                chart_changed, width = payload
                # v1.2.43：图表数据/几何未变 → 只重绘价格条带（分时图每 tick 零重绘）；
                # 动画期间恰有更新（_chart_changed）→ 整窗重绘一次重画图表。
                #
                if should_draw_chart(True, chart_changed):
                    self._partial_price = None
                    self._repaint_now()
                else:
                    self._partial_price = price_update_rect(width)
                    self._repaint_now(*self._partial_price)
            elif kind == "layout_minmax":
                # v1.2.35/37：展开态恢复 MIN_H/MAX_H，收起态把 max 锁回收起高度
                try:
                    if payload:
                        self.setMinimumHeight(MIN_H)
                        self.setMaximumHeight(MAX_H)
                    else:
                        self.setMaximumHeight(self._height_collapsed())
                except Exception as _swallow_exc:
                    log_swallow("stock_float_window._anim_tick_inner", _swallow_exc)
                # v1.4.59（S2 第二处）：高度收口之后必须回移一次。
                # 贴底展开分时图时，卡片是**向下**长高的，底部整块会长到 Dock 之下；
                # 而 `_clamp_to_screen` 的判据是 `not ag.intersects(g)` —— 还剩 1px
                # 在屏内就不管，于是这条路径既不钳也不回移，越界坐标还会被下一次
                # save_config 写盘固化（重启仍落在屏外）。宽度变化早就有这一步
                # （_auto_width 里调 _keep_on_screen），高度变化一直缺。
                self._keep_on_screen()
            elif kind == "glass_mask":
                # v1.2.26：动画结束帧强制对齐玻璃层圆角 mask
                try:
                    self._sync_glass_frame(force_mask=True)
                except Exception as _swallow_exc:
                    log_swallow("stock_float_window._anim_tick_inner", _swallow_exc)

    # ---------- 跌破整数关口闪烁 ----------
    def _crossed_key_level(self, old_price, new_price):
        """向下跌破过程中是否跨过重要整数关卡（50 的整数倍：50/100/150/200/250...）。
        只关心向下区间 (new, old] 内是否存在 50 的倍数；
        普通整数位（如 410）不属于关键关卡，不触发闪烁。
        v1.2.40：纯逻辑下沉 sf_animator.crossed_key_level，此处仅薄委托。"""
        return crossed_key_level(old_price, new_price)

    def _start_price_flash(self):
        """价格跌破整数时触发：短暂高亮闪烁后恢复，每次跌破只闪一次
        v1.2.48：截止时刻/起始相位记录下沉 AnimDriver.start_price_flash，
        本方法仅启动定时器与重绘。
        v1.2.71：窗口不可见时不启动定时器（详见 _animations_suspended）。"""
        if self._animations_suspended():
            return
        self._anim_driver.start_price_flash(time.monotonic(), FLASH_DUR)
        self._flash_timer.start()
        self.update()

    def _flash_tick(self):
        """闪烁相位交替：on 显示高亮色，off 恢复正常色，到期停止
        v1.2.48：相位决策下沉 AnimDriver.flash_tick（返回 active/相位），
        本方法仅负责停表与重绘。"""
        try:
            active, _on = self._anim_driver.flash_tick(time.monotonic())
            if not active:
                self._flash_timer.stop()
            self._repaint_now()
        except Exception as exc:
            log_line("flash error: %r" % (exc,))

    def _maybe_trigger_drop_flash(self, pct):
        """12.x v1.2.11：在实时行情刷新回调中判断跌幅阈值穿越并启动下跌闪烁。
        基准为涨跌幅为负时的绝对值；两档独立判断、独立冷却。
        v1.2.48：阈值判定与冷却更新下沉 AnimDriver.check_drop_flash。
        仅当功能开关开启且窗口已就绪时生效；异常一律静默，不打断行情。"""
        try:
            if not feature_enabled("drop_flash"):
                return
            if self._anim_driver.check_drop_flash(
                    pct, time.monotonic(), FLASH_DROP_LEVELS, FLASH_DROP_COOLDOWN):
                self._start_drop_flash()
        except Exception as exc:
            log_line("drop flash check error: %r" % (exc,))

    def _animations_suspended(self):
        """v1.2.71：窗口当前不可见 → 不应启动任何动画定时器。

        缩到 Dock（_minimized）或从 Dock 恢复前（!isVisible）时，行情线程仍在
        按 5s 低频拉取，_apply_quote 会照常走动画分支 —— 每 5s 把 16ms/350ms
        的动画定时器重新拉起一次，恰好抵消 v1.2.39「隐藏期间省电」的意图；
        恢复可见时还会一次性补跑一批已过期的闪烁。
        判定失败（异常）时按"不挂起"处理，宁可多跑一次动画也不吞掉功能。"""
        try:
            if getattr(self, "_minimized", False):
                return True
            return not self.isVisible()
        except Exception:
            return False

    def _start_drop_flash(self):
        """启动下跌闪烁：正常色与下跌红交替 3 个来回（FLASH_DROP_PHASES 个相位），
        结束后 _drop_flash_tick 自动停表并恢复 _trend_color() 对应的正确颜色。
        v1.2.48：相位状态下沉 AnimDriver.start_drop_flash，本方法仅启动定时器与重绘。
        v1.2.71：窗口不可见时直接返回，不启动定时器（详见 _animations_suspended）。"""
        if self._animations_suspended():
            return
        self._anim_driver.start_drop_flash()
        self._flash_drop_timer.start()
        self.update()

    def _drop_flash_tick(self):
        """下跌闪烁相位交替：灭(正常色) -> 亮(下跌红) -> 灭 ... 相位数用尽即停。
        仅改显示颜色相位，行情刷新/动画 timer 独立运行，互不阻塞。
        v1.2.48：相位计数/翻转决策下沉 AnimDriver.drop_flash_tick，
        本方法仅负责停表与重绘。"""
        try:
            active, _on = self._anim_driver.drop_flash_tick(FLASH_DROP_PHASES)
            if not active:
                self._flash_drop_timer.stop()
            self._repaint_now()
        except Exception as exc:
            log_line("drop flash tick error: %r" % (exc,))
            # v1.2.58：异常时停表 + 恢复状态，防止永久闪烁
            self._flash_drop_timer.stop()
            self._anim_driver.flash_drop_on = False

    # ---------- 「目标价」功能（v6 Step 4/6/7） ----------
    # ---------- v1.4.14 帧率优先：分时图末端呼吸灯的独立时钟 ----------
    def _dot_should_run(self):
        """呼吸灯此刻是否需要独立刷新。

        返回 False 的几种情形本来就没有可见动画，停表不损失任何帧率：
          · 图表收起（`_chart_expanded` 为假）→ 根本不画分时图；
          · 无分时数据 → 画的是"加载中/市场状态"文字，不会动；
          · 图带太矮（< CHART_MIN_DRAW_H）→ v1.3.51 闸门整幅不画；
          · 数据滞后（`_minute_stale`）→ v1.4.7 刻意把呼吸冻结成最小态，
            再刷也是同一个静止灯形，刷起来反而冒充"实时"；
          · 缩到 Dock / 窗口不可见 → 没有观众。
        判定自身出异常时一律返回 True：帧率优先，宁可多刷也不能把动画刷没了。

        **本方法每 16ms 被 `_dot_tick` 调一次，所以只做纯内存/纯属性的廉价判断。**
        "是不是交易时段"这条闸门不在这里 —— 见 `_dot_armed()`（v1.4.15 F2 拆出去）。
        """
        try:
            if self._minimized or not self.isVisible():
                return False
            if not getattr(self, "_chart_expanded", False):
                return False
            if not self.minute_points:
                return False
            if getattr(self, "_minute_stale", False):
                return False
            if self.height() - CHART_TOP < CHART_MIN_DRAW_H:
                return False
        except Exception:
            return True
        return True

    def _dot_armed(self):
        """时段闸门：非交易时段不给呼吸灯时钟（低频求值，只在状态复核时判）。

        为什么单独拆出来（v1.4.15 F2）：`_is_trading_time()` 会走
        `sf_calendar.is_holiday → _peek_month`，而 `_peek_month` 在内存缓存未命中时
        会 `_load_month_cache(ym)` 读一次磁盘。2026 年走内置表没事；**跨到 2027 年
        且未来月份日历还没拉到时**，把它放在 16ms 热路径上就是每秒 60 次磁盘读。
        交易时段一天只翻转四次，交给 `_sync_dot_timer()`（由 `_poll` 与
        `_on_session_boundary` 驱动）判就够。

        语义不变（v1.4.14 定的）：呼吸灯是"这条走势是实时的"这个暗示，不是装饰 ——
        v1.4.7 已定下"数据滞后即冻结、绝不冒充实时"。收盘后没有实时数据，满帧呼吸
        反而在说谎；且实测不加这道闸门，盘后 CPU 从 0.62% 单核涨到 25.72%（41 倍）。
        """
        try:
            return bool(self._is_trading_time())
        except Exception:
            return True

    def _sync_dot_timer(self):
        """按当前状态武装/停掉呼吸灯时钟（幂等，任何状态变化处都可调用）。"""
        try:
            if self._dot_armed() and self._dot_should_run():
                if not self._dot_timer.isActive():
                    # 起表时对齐已见序号：停表期间发生的 resizeEvent 不能算作「本周期已覆盖」。
                    self._dot_seen_resize_seq = self._resize_paint_seq
                    self._dot_timer.start()
            elif self._dot_timer.isActive():
                self._dot_timer.stop()
        except Exception:
            pass

    def _repaint_now(self, *rect):
        """16ms 级动画统一的"请求一帧"入口（v1.4.14 帧率优先）。

        呼吸灯时钟在跑的时候，它每 16ms 已经请求过一次**整窗**帧，
        价格滚动 / 白闪 / 红闪 / 彩虹再各自 update() 就是重复排队：
        两个同频定时器落在不同事件循环轮次里，Qt 合并不了。
        实测 price 场景 120 次绘制/秒、rainbow 场景 125 次/秒，
        而 60Hz 屏最多显示 60 帧 —— 多出来的一半纯属白画。

        所以规则是：**呼吸灯当主画钟，其余动画只推进自己的状态。**
        呼吸灯没在跑（图表收起 / 无数据 / 滞后冻结 / 不可见）时，
        各动画照旧自己请求重绘，局部脏区那条省法也原样保留。

        **盘中这条局部省法是失效的，不是漏了 —— 这是"帧率第一"的定价。**
        v1.2.43 的价格条带局部重绘要生效，前提是那一帧的损坏区恰好只有条带；
        而呼吸灯要满 60 帧就必须每 16ms 请求一次整窗帧（它的相位按墙上时钟算，
        且画在分时图里，条带脏区按设计绝不覆盖图表区）。主钟每帧都是整窗，
        被它兜住的动画帧自然也都是整窗。实测盘中稳态 12 秒：
        `update()` 整窗 734 / 局部 0，实际绘制 整窗 735 / 局部 0 —— 局部路径
        一次都没走到。于是盘中绘制成本 = 约 62 帧/秒 × 单帧整窗 ~2.8ms，
        占总 CPU 的六成以上；v1.4.14 把 CPU 从 17% 抬到约 28~30% 单核就是这笔。
        想把它压回去，只能改"每帧要画的图元本身"（折线按可见像素分级简化等），
        加中间位图缓存那条路已实测更慢，详见 DELIVERY_CHECKLIST v1.4.14 追加节。
        """
        try:
            if getattr(self, "_dot_timer", None) is not None and self._dot_timer.isActive():
                return
        except Exception:
            pass
        if rect:
            self.update(*rect)
        else:
            self.update()

    def _dot_tick(self):
        """呼吸灯一帧：请求重绘，让 _draw_chart_dot 的相位按 16ms 往前推。

        刻意走整窗 update()，不给图表区单开局部脏区路由：paintEvent 目前只有
        "价格条带"一条局部路由，且按设计绝不侵入图表区。为图表另开一条要重画
        渐变与折线、省不下多少，却会把脏区判定的复杂度引进来。
        每帧成本目前没有缓存可压 —— 分时图整层位图缓存实测更慢（29.56% →
        30.22% 单核）且已回退，见 DELIVERY_CHECKLIST v1.4.14 追加节。
        """
        if not self._dot_should_run():
            # v1.4.15 F4：停表这一路必须自己补一次重绘。
            # 同一周期里动画帧可能已经因为"主钟会画"而跳过了 `_repaint_now()`，
            # 两边都不画就会丢一帧（实测最坏 16ms，发生在时段/图表状态切换瞬间）。
            try:
                self._dot_timer.stop()
            except Exception:
                pass
            self.update()
            return
        # 收展动画进行中，动画每帧的 resize 已经排过一次整窗帧（resizeEvent 里），
        # 自己再排一次就是同一个 vsync 周期里的第二帧。只在「上个节拍到现在确有
        # resizeEvent」时才跳过：动画某帧高度没变化、没有 resizeEvent 时，这里照常
        # 排帧，所以呼吸灯不会因此少一帧。只限收展动画：拖动拉伸时 resizeEvent
        # 节奏不均，跳过会让呼吸灯跟着变稀，那条路径保持原样。
        seq = self._resize_paint_seq
        covered = seq != self._dot_seen_resize_seq
        self._dot_seen_resize_seq = seq
        if covered and self._layout_anim_to != self._layout_anim_from:
            return
        self.update()

    def _targets_enabled(self):
        return feature_enabled("targets")

    def _current_price(self):
        """当前用于**目标价判定**的价格（不是展示价）。

        R48：非交易时段（盘前/休市/收盘）腾讯接口返回 0，`_quote_zero_fallback`
        会用分时缓存的最后一点填充展示价 —— 盘前那还是**上一交易日**的收盘价。
        它不是今天的成交价，拿它判定会让「跌到 X」一开盘前就被昨收误触发。
        兜底期间只认真实行情价；真实价缺失就返回 0（_valid_price 判否 →
        不下结论），宁可不提醒也不误报。

        R47：非兜底时同样优先取行情原始价，而不是 _disp_price —— 后者是逐位
        滚动动画的**中间帧**。价格从 64.00 跳到 65.00 时动画会路过 64.50，
        用动画值判定会让「跌到 64.50」被一个从未成交过的价格触发。
        """
        if getattr(self, "_price_is_fallback", False):
            try:
                rp = float(getattr(self, "_real_price", 0) or 0)
            except (TypeError, ValueError):
                rp = 0.0
            return rp if rp > 0 else 0.0
        for getter in (lambda: getattr(self, "_real_price", None),
                       lambda: (self.quote or {}).get("price"),
                       lambda: self._disp_price):
            try:
                v = float(getter() or 0)
            except (TypeError, ValueError):
                continue
            if v > 0:
                return v
        return 0.0

    def _bell_state(self):
        """铃铛三态：none（无目标价）/ pending（有待触发）/ fired（已提醒）。"""
        if not self._targets_enabled():
            return "none"
        alerts = self._safe_alerts()
        if not alerts:
            return "none"
        # 只要还有待触发目标，铃铛保持黄色，直观提示仍有提醒在等待；
        # 全部已触发时才转为灰色。
        if any(a.get("state") != STATE_HIT for a in alerts):
            return "pending"
        return "fired"

    def _show_btn_blocked_feedback(self, name):
        """互斥入口被点击时给可见反馈，避免用户误判为按钮失灵。"""
        text = "请先关闭目标价窗口" if name == "edit" else "请先关闭替换股票窗口"
        try:
            QToolTip.showText(self.mapToGlobal(QPoint(self.width() // 2, BTN_CY + 20)), text, self)
            QTimer.singleShot(2200, QToolTip.hideText)
        except Exception as _swallow_exc:
            log_swallow("stock_float_window._show_btn_blocked_feedback", _swallow_exc)
        log_line("btn blocked: %s" % name, "INFO")

    def _is_btn_disabled(self, name):
        """v1.2.87：右上角入口互斥判定。

        - 替换股票编辑器（EditorWindow）开启时，「目标价」铃铛入口禁用（置灰 + 不可点）；
        - 目标价面板（TargetPanel）开启时，「替换股票」编辑图标入口禁用（置灰 + 不可点）。
        返回 True 表示该入口当前不可用（对应图标已置灰）。
        """
        try:
            editor_open = bool(self._editor is not None and self._editor.isVisible())
        except Exception:
            editor_open = False
        try:
            target_open = bool(self._target_panel is not None
                               and self._target_panel.isVisible())
        except Exception:
            target_open = False
        if name == "edit":
            return target_open
        if name == "bell":
            return editor_open
        return False

    def _start_rainbow(self):
        """目标价命中后播放 6 秒流动彩虹；只影响价格大字，不改变通知或目标价状态。"""
        if not self._targets_enabled() or self._animations_suspended():
            return
        self._anim_driver.start_rainbow(time.monotonic(), 6.0)
        self._rainbow_timer.start()
        self.update()

    def _rainbow_tick(self):
        """推进流动彩虹；到期停止计时并恢复正常涨跌颜色。"""
        try:
            if self._anim_driver.rainbow_active(time.monotonic()):
                self._repaint_now()
            else:
                self._rainbow_timer.stop()
                self._repaint_now()
        except Exception as exc:
            log_line("rainbow tick error: %r" % (exc,), "WARNING")
            try:
                self._rainbow_timer.stop()
            except Exception:
                pass

    def _sync_target_panel(self):
        """把实时目标价同步到已打开的面板（若有）。

        R46：面板只在打开时载入一次快照，此后完全不感知后台变化。某条在面板
        打开期间被触发时，列表仍显示"待触发 + 距目标%"，用户会以为提醒没生效、
        重复添加同价位目标。
        """
        if not self._targets_enabled():
            return
        panel = getattr(self, "_target_panel", None)
        if panel is None:
            return
        try:
            if panel.isVisible():
                panel.sync_alerts(self._safe_alerts())
        except Exception:
            pass

    def _prefetch_calendar(self):
        """后台预热「本月 + 次月」休市日（仅 >2026 需要，2026 用内置表）。

        R32：>2026 的休市日需从深交所月表补，且调用线程不得发网络请求。
        提前预热可让用户首次设置目标价时就拿到正确日历，而不是降级成
        「仅按周末判断」。失败静默，不影响启动。
        """
        try:
            now = _beijing_now()
            yms = []
            for i in range(2):
                y, m = now.year, now.month + i
                if m > 12:
                    y, m = y + 1, m - 12
                if y > 2026:
                    yms.append("%04d-%02d" % (y, m))
            prefetch_months_async(yms)
            return calendar_sync_status(yms)
        except Exception as exc:
            log_line("calendar prefetch error: %r" % (exc,))
            return {}

    def _init_targets(self):
        """启动恢复（D2/M-8）：过期静默丢弃、未过期保留、已越线补提醒一次。"""
        if not self._targets_enabled():
            return
        self._prefetch_calendar()
        try:
            raw = load_alerts(self.code)   # v1.4.59（S4）：只取当前这只股票
            if not raw:
                self._alerts = []
                return
            # R47：先把落在非交易日的生效日顺延（跨年/未来月份日历未缓存时
            # 可能落到元旦、春节这类休市日，否则目标价会整天不触发还被静默清空）。
            raw, rolled = roll_alerts_to_trading_day(raw)
            now = _beijing_now()
            price = self._current_price()
            kept, reminders = recover_alerts(
                raw, now, price, decimals=price_decimals(self.code))
            recovered_persisted = True
            # 启动恢复产生了内存清洗/状态变更时，必须先成功写回再替换运行态；
            # 写盘失败时保留 raw，避免“界面已恢复、磁盘仍旧数据”导致下次重复提醒。
            # R46：全部过期时 kept 为空，原先 `if kept:` 会跳过落盘，
            # 磁盘里留着永不生效的死条目 —— 铃铛显示 none（内存已空），
            # 但打开面板又看到那条目标价，两处说法不一致。
            if kept != raw or rolled:
                recovered_persisted = bool(self._persist_alerts(kept))
                self._after_save_config(recovered_persisted)
            # R46：写盘失败时不能原样回退 raw —— 里面可能混着本该被 recover
            # 丢弃的过期条目，而 _arm_clear_timer 只武装未来时刻，它们既不会被
            # 清掉也不会生效，铃铛常亮、面板看得到，直到重启才消失。
            # 过期的条目不可能触发提醒（in_effect_window 已拦截），丢掉是安全的。
            self._alerts = kept if recovered_persisted else drop_expired(raw, now)
            name = self.quote.get("name") if self.quote else ""
            for r in reminders if recovered_persisted else []:
                _notify_deliver(target_notification_title(name, self.code), "", r["text"],
                                semantic_key(r["direction"], r["target"], self.code),
                                on_click=self._restore_window)
            # R48：启动恢复路径也要确保已请求过通知授权。此前只有面板「立即生效」
            # 会请求，若那次请求因系统原因静默失败（或用户是升级前设的目标价），
            # 之后永远不再重试 —— 用户设了目标价却一次通知都收不到。
            if self._safe_alerts():
                self._request_notify_auth()
            self._arm_clear_timer()
            self.update()
        except Exception as exc:
            log_line("init targets error: %r" % (exc,), "WARNING")
            self._alerts = []

    def _set_targets(self, alerts):
        """保存目标价（面板「立即生效」）：与实时状态合并后更新内存 + 落盘 +
        重新武装清空定时器 + 请求通知授权。返回是否保存成功。

        v1.2.77 自审修复：
          D16 合并（merge_alerts）保证面板打开期间已触发（hit）的目标价不被回退成
              pending —— 否则下一个 tick 会重复弹提醒；
          D7  合并结果未变时跳过落盘；定时器、授权复核与刷新仍照常执行；
          D14 清空改由独立的 _clear_targets 处理，不再复用本函数。
        """
        if not self._targets_enabled():
            return False
        # 保存/合并/过期过滤的纯事务下沉到协调器；主窗只保留 Qt 定时器和刷新编排。
        ok, committed, _changed = TargetLifecycleCoordinator(self._persist_alerts, _beijing_now).save(
            alerts, self._safe_alerts())
        self._after_save_config(ok)
        if not ok:
            return False
        self._alerts = committed
        self._arm_clear_timer()
        self._request_notify_auth()
        self.update()
        return True

    def _request_notify_auth(self):
        """请求通知授权（结果不回溯阻断保存，v6 §5.2）。"""
        try:
            request_authorization()
        except Exception:
            pass

    def _clear_targets(self):
        """清空目标价（面板「清空」，与 _set_targets 解耦）：落盘 + 清内存 +
        停定时器，**不请求通知授权**（D14/D20）。返回是否成功。

        R42-2：先落盘成功再动内存与界面，失败时保持原样可重试。
        """
        if not self._targets_enabled():
            return False
        try:
            # 清空的持久化事务由协调器负责；空状态不会写入配置文件。
            ok, committed, _changed = TargetLifecycleCoordinator(self._persist_alerts, _beijing_now).clear(
                self._safe_alerts())
            self._after_save_config(ok)
            if not ok:
                return False
            self._alerts = committed
            for _t in ("_clear_timer",):
                try:
                    getattr(self, _t).stop()
                except Exception:
                    pass
            self.update()
            return True
        except Exception as exc:
            log_line("clear targets error: %r" % (exc,), "WARNING")
            return False

    def _adopt_targets_for(self, code):
        """把存储里属于 `code` 的目标价装进内存（切股与启动恢复共用同一口径）。

        过期的条目顺手丢掉并写回 —— 与 `_init_targets` 同一道理：留着它们铃铛会亮、
        面板看得到，却永远不会触发，两处说法不一致。
        """
        if not self._targets_enabled():
            self._alerts = []
            return
        try:
            stored = load_alerts(code)
            now = _beijing_now()
            kept = [a for a in stored if not expired(a, now)]
            self._alerts = kept
            if len(kept) != len(stored):
                self._after_save_config(self._persist_alerts(kept))
            self._arm_clear_timer()
        except Exception as exc:
            log_line("adopt targets failed (%s): %r" % (code, exc), "WARNING")
            self._alerts = []

    def _persist_alerts(self, alerts):
        """目标价落盘的唯一入口（v1.4.59，S4 彻底解）。

        带上当前 code 是为了"只替换这只股票的条目" —— 别的股票在存储里的条目
        原样留着。以前切股等于把上一只的目标价整片删掉，而这是用户一条条手录的
        核心数据；现在删除只可能来自面板的「清空」（那也只清当前这只）。
        """
        return save_alerts(alerts, code=self.code)

    def _safe_alerts(self):
        """取 _alerts；兼容未完全初始化的对象（测试桩）与未设置的边界。"""
        try:
            return self._alerts
        except Exception:
            return []

    def _evaluate_received_quote(self, quote):
        """业务逐笔判断，显示仍允许合批；旧日期与非交易时段不触发。"""
        if not self._targets_enabled() or not self._is_trading_time():
            return
        if not isinstance(quote, dict) or not quote_is_current(quote):
            return
        code = quote.get("code")
        if isinstance(code, str) and _normalize_code_6(code) != _normalize_code_6(self.code):
            return
        try:
            price = float(quote["price"])
        except (KeyError, TypeError, ValueError):
            return
        if price > 0 and math.isfinite(price):
            self._evaluate_targets(price)

    def _evaluate_targets(self, price):
        """行情 tick：对 pending 目标价判定越线；命中即通知 + 彩虹 + 持久化。"""
        if not self._targets_enabled():
            return
        alerts = self._safe_alerts()
        if not alerts:
            return
        try:
            # R47：每个 tick 先纠正落在非交易日的生效日（跨年/未来月份日历未
            # 缓存时会落到休市日）。只有真正非交易日才会改，正常交易日零开销。
            alerts, rolled = roll_alerts_to_trading_day(alerts)
            if rolled:
                ok = self._persist_alerts(alerts)
                self._after_save_config(ok)
                if not ok:
                    return
                self._alerts = alerts
            out, fired = evaluate_tick(
                alerts, price, decimals=price_decimals(self.code))
            if not fired:
                return
            # 命中状态先落盘成功，再更新内存和发送通知；保存失败时保留 pending，
            # 避免下次 tick 因“内存已 hit、磁盘仍 pending”造成状态分裂。
            ok = self._persist_alerts(out)
            self._after_save_config(ok)
            if not ok:
                return
            self._alerts = out
            self._sync_target_panel()
            self._start_rainbow()
            name = self.quote.get("name") if self.quote else ""
            for f in fired:
                _notify_deliver(target_notification_title(name, self.code), "", f["text"],
                                semantic_key(f["direction"], f["target"], self.code),
                                on_click=self._restore_window)
            self.update()
        except Exception as exc:
            log_line("evaluate targets error: %r" % (exc,), "WARNING")

    def _arm_clear_timer(self):
        """专用清空定时器（D1）：按各 alert 的 session_date 15:00:30 武装单发。"""
        if not self._targets_enabled():
            return
        alerts = self._safe_alerts()
        if not alerts:
            return
        now = _beijing_now()
        earliest = None
        for a in alerts:
            sd = a.get("session_date")
            if not sd:
                continue
            dt = _clear_dt(sd)
            if dt is None:
                continue  # R33：session_date 不可解析，跳过（不再抛 ValueError）
            verdict = is_trading_day_confirmed(dt.date())
            if verdict is None:
                # 官方日历尚未就绪：保留目标价，不武装错误日期的清空定时器。
                continue
            if dt > now and (earliest is None or dt < earliest):
                earliest = dt
        if earliest is None:
            # R46：所有条目都已过清空点（定时器漏触发 / 休眠跨过 15:00:30 /
            # 启动恢复落盘失败保留了旧数据）。此时没有任何未来时刻可武装，
            # 原实现直接 return —— 死条目会一直挂在列表里、铃铛一直亮到重启。
            if any(expired(a, now) for a in alerts if isinstance(a, dict)):
                self._clear_expired()
            return
        ms = int((earliest - now).total_seconds() * 1000)
        if ms <= 0:
            ms = 1   # QTimer 间隔必须为正：0 等价于“立即触发”，抬到 1ms 保持到点单发
        try:
            self._clear_timer.stop()
            self._clear_timer.start(ms)
        except Exception:
            pass

    def _clear_expired(self):
        """清空定时器到点：丢弃已过 15:00:30 的目标价，保留未来 session_date。"""
        try:
            now = _beijing_now()
            alerts = self._safe_alerts()
            kept = [a for a in alerts if not expired(a, now)]
            if len(kept) != len(alerts):
                # 先写盘，成功后才替换内存；清空落盘失败时保留原状态，
                # 避免重启后旧目标再次出现而界面此前已显示为空。
                if self._persist_alerts(kept):
                    self._alerts = kept
                    self._sync_target_panel()
                    self.update()
            self._arm_clear_timer()
        except Exception as exc:
            log_line("clear expired error: %r" % (exc,), "WARNING")

    def _raise_tool_window(self, tool):
        """把工具窗抬到主窗**之上**，让主窗的周期置顶压不住它。

        v1.4.29 先修一个潜伏了几十个版本的空转：本方法裸调 `_objc_deps()`，而这个名字
        从来没有导入进本模块（它定义在 sf_icons，sf_glass 才有名字）→ 每次调用都在
        `setLevel_` 之前抛 NameError，又被下面的 `except: pass` 吞掉，**整段抬层从写下
        那天起一次也没执行过**（真机实测：面板/编辑器层级恒为 8、相对顺序恒为"主窗在
        上"）。之所以看不见：面板按 `_place_panel` 永远贴在与主窗不重叠的一侧。
        教训写进 import 处了 —— 兜异常的 except 至少要留一条限流日志。

        为什么这段以前"从没崩过"：它在 NameError 处就返回了，根本没走到 objc。
        修活之后离屏（offscreen/minimal）环境下 `winId()` 不是 NSView 指针，
        转 objc_object 再 `.window()` 会**直接段错误**（v1.4.29 复核 F1：全量测试
        进程 exit=139 就是这么来的），所以第一行必须先问 `native_cocoa_active()`。

        层级取"主窗 + 1"（`sf_glass.TOOL_WINDOW_LEVEL`）而不是与主窗同为 25：同级时
        "谁在上"只由最后一次 orderFront 决定，本方法的 orderFront 之后主窗自己还会再
        前置若干次；用更高一层后先后由层级决定，与调用顺序无关。
        """
        if not native_cocoa_active():
            return
        try:
            tool.raise_()
            objc, c_void_p = _objc_deps()
            ns_window = objc.objc_object(c_void_p=int(tool.winId())).window()
            if ns_window is not None:
                ns_window.setLevel_(TOOL_WINDOW_LEVEL)
                ns_window.orderFrontRegardless()
        except Exception as exc:
            # 限流留痕：这条日志就是"抬层又静默失效"的唯一探测器
            log_line_throttled("raise_tool_window",
                               "raise tool window failed: %r" % (exc,), "WARNING")

    def _raise_open_tool_windows(self):
        """把已打开的工具窗抬回主窗之上 —— 只在主窗刚被重新前置/置顶之后调用。

        v1.4.28 跟手性（实测依据见 outputs/触控跟手性延迟画像-v1.4.27-2026-09-23.md）：
        这一步原来写在 `_sync_open_tool_windows()` 里，而 `moveEvent` 每收到一个
        鼠标移动事件就调一次 —— 目标价面板开着拖动时，每拍因此多花 1.2~1.6ms
        （整拍实测均值 1.6ms、最差 6.9ms，对比无面板时的 0.44ms）。而它守的是
        "主窗 orderFront 之后，面板不能沉到主窗底下"，主窗的 orderFront 只发生在
        `_force_top` 完整路径里，所以抬层跟着那条路径走就够了；**跟位**才需要
        每次移动都做（`_place_panel` 实测 30~150µs，保留）。
        """
        for tool in (getattr(self, "_target_panel", None),
                     getattr(self, "_editor", None)):
            if tool is None:
                continue
            try:
                if tool.isVisible():
                    self._raise_tool_window(tool)
            except Exception:
                pass

    def _sync_open_tool_windows(self):
        """主窗移动、跨屏或恢复后，让已打开工具窗重新贴回主窗。

        **不带形参**：全仓按 `lambda: None` 给本方法打桩（tests/test_power_v1410.py:525、
        tests/test_v1426_glass_zorder.py:68），一加形参这些替身就撞 TypeError ——
        与 `_force_top` 的同款教训（v1.4.27 复核 F2）一致。

        抬层不在这里做（v1.4.28）：拖动的每一拍都做 WindowServer 抬层是纯开销，
        需要抬层的时刻只有"主窗刚被前置"，那在 `_force_top` 里紧跟着调
        `_raise_open_tool_windows()`。
        """
        panel = getattr(self, "_target_panel", None)
        if panel is not None:
            try:
                if panel.isVisible():
                    self._place_panel(panel)
            except Exception:
                pass
        editor = getattr(self, "_editor", None)
        if editor is not None:
            try:
                if editor.isVisible():
                    editor._anchor = self.pos()
                    editor._anchor_w = self.width()
                    editor._move_near()
            except Exception:
                pass

    def _close_target_panel(self):
        """关闭并丢弃缓存的「目标价」面板（切股或退出时调用）。

        切股后会按新股票重新载入目标价；关闭旧面板只为避免展示上一只股票的编辑状态。
        """
        panel = getattr(self, "_target_panel", None)
        self._target_panel = None
        if panel is None:
            return
        try:
            panel.close()
        except Exception:
            pass

    def _open_target_panel(self):
        """打开「目标价」设置面板（参考 EditorWindow 定位，主窗右侧 +12）。

        v1.2.77 自审修复：
          D24 功能关闭时不打开，避免点了「立即生效」却静默失效；
          D6  已有面板且有未生效修改时不静默丢弃，保留并提示；
          D4  定位做屏幕边界收敛，右侧放不下则翻到左侧，不再生成到屏幕外。
        """
        try:
            if not self._targets_enabled():
                log_line("open target panel skipped: targets disabled", "INFO")
                return
            # M1：互斥下沉到入口内部。顶部按钮已有置灰拦截、右键菜单已置灰，
            # 但未来任何新入口都可能绕过 UI 层 —— 此处做最后一道闸，
            # 杜绝「编辑器 + 面板」双弹窗并存（并存时编辑图标已被置灰，
            # 用户反而关不掉编辑器，只能去点编辑器的 ✕）。
            if self._is_btn_disabled("bell"):
                log_line("open target panel blocked: editor open", "INFO")
                return
            from target_panel import TargetPanel
            panel = getattr(self, "_target_panel", None)
            if panel is not None:
                # R34-17：切股后缓存的面板已失效（标题与列表仍是上一只股票），
                # 且保存会被 D25 挡下形成死局。代码不一致时一律重建。
                try:
                    stale = (getattr(panel, "code", None) != self.code)
                except Exception:
                    stale = True
                if stale:
                    self._close_target_panel()
                    panel = None
            if panel is not None:
                try:
                    # D6：只要有未生效修改就一律保留提示，不因面板不可见而静默丢弃
                    if panel.has_unsaved_changes():
                        panel.warn_unsaved()
                        panel.show()
                        self._raise_tool_window(panel)
                        panel.activateWindow()
                        return
                    # 面板已打开且没有任何未生效修改：铃铛作为开关，第二次点击直接收起。
                    # 只有已经隐藏的旧面板才继续走重建路径，避免多一次点击反而重新弹出。
                    if panel.isVisible():
                        self._close_target_panel()
                        return
                    panel.close()
                except Exception as _swallow_exc:
                    log_swallow("stock_float_window._open_target_panel", _swallow_exc)
            name = self.quote.get("name") if self.quote else ""
            panel = TargetPanel(self.code, name, self._set_targets,
                                self._clear_targets,
                                current_code_fn=lambda: self.code,
                                current_price_fn=lambda: self._current_price(),
                                # v1.2.87：与主窗对齐 + 互斥 + 校验所需的运行时回调
                                width=self.width(),
                                # “已触发”仅改变铃铛与价格区提示，不再锁死编辑/保存。
                                # 同槽位重复提醒由 merge_alerts 保留 hit 状态负责。
                                # v1.2.87 补丁（M5）：删除 trading_fn 通道。
                                # 它原用于面板焦点策略，但传入的是 _is_trading_time()
                                # （回答"此刻是否在连续竞价"），被当成"今天是否交易日"用 ——
                                # 于是午休/集合竞价/收盘后全被判成非交易时段。
                                # S2 已让焦点固定落在输入框，该参数再无消费方；
                                # 保留只会留下一个语义错误的诱饵，故整体移除。
                                # 注：_is_trading_time 在本文件其余调用处（轮询频率、
                                # 分时抓取、休市提示、nap 策略）语义均正确，未改动。
                                # 输入限制只使用行情源明确给出的涨跌停价。无官方字段时
                                # 仍可设置目标价，避免新股等特殊规则被本地估算误拦截。
                                limits_fn=lambda: official_limit_prices(self.quote))
            panel.set_surface_owner(self)
            self._target_panel = panel
            for status in self._prefetch_calendar().values():
                if status in ("syncing", "degraded"):
                    panel.show_calendar_sync_hint(status)
                    break
            # M4 配套：面板带 WA_DeleteOnClose，销毁后必须同步清引用，
            # 否则 self._target_panel 指向已删除的 C++ 对象，后续 isVisible()
            # 会抛 RuntimeError（虽被 try 兜住，但会误判为 stale 而重建）。
            def _clear_tp(_destroyed=None, obj=panel):
                if self._target_panel is obj:
                    self._target_panel = None
            panel.destroyed.connect(_clear_tp)
            panel.adjustSize()          # 先算出真实高度，便于做边界收敛
            self._place_panel(panel)
            # 显示前应用无权限的统一表面设置（关闭 Tool 原生阴影）。
            try:
                panel.prepare_surface()
            except Exception as _swallow_exc:
                log_swallow("stock_float_window._open_target_panel", _swallow_exc)
            panel.show()
            self._raise_tool_window(panel)
            panel.activateWindow()
        except Exception as exc:
            log_line("open target panel error: %r" % (exc,), "WARNING")

    def _place_panel(self, panel):
        """面板定位（D4）：优先主窗右侧 +12；右侧空间不足翻到左侧；再做边界收敛。"""
        try:
            gx = self.x() + self.width() + 12
            gy = self.y()
            w = panel.width() or 244
            h = panel.height() or 240
            screen = None
            try:
                screen = self.screen()
            except Exception:
                screen = None
            if screen is None:
                try:
                    from PyQt6.QtWidgets import QApplication
                    screen = QApplication.primaryScreen()
                except Exception:
                    screen = None
            if screen is not None:
                # 注意：QRect.right() == left + width - 1，直接用会少 1px，
                # 这里改用显式左右边界，避免收敛后贴边溢出。
                ag = screen.availableGeometry()
                left = ag.x()
                right = ag.x() + ag.width()
                top = ag.y()
                bottom = ag.y() + ag.height()
                if gx + w > right:
                    gx = self.x() - w - 12      # 右侧放不下 → 放到主窗左侧
                gx = max(left, min(gx, right - w))
                gy = max(top, min(gy, bottom - h))
            panel.move(int(gx), int(gy))
        except Exception:
            try:
                panel.move(self.x() + self.width() + 12, self.y())
            except Exception as _swallow_exc:
                log_swallow("stock_float_window._place_panel", _swallow_exc)

    def _setup_reopen_handler(self):
        """接管 NSApplicationDelegate 的 reopen：点击 Dock 图标时恢复悬浮窗。
        使用自定义转发代理（8.1 修复）：只接管 reopen，其余 delegate 消息转发给
        Qt 原 delegate，不再依赖 Qt 私有类名，避免猴子补丁随 Qt 升级静默失效。"""
        try:
            cls = _ensure_reopen_class()
            if cls is None:
                return
            from AppKit import NSApp
            if getattr(self, "_reopen_proxy", None) is None:
                proxy = cls.alloc().init()
                # 首次接管：保存 Qt 原 delegate 并替换为代理。此后仅校验 _orig，
                # 若 Qt 内部重设了 delegate（极罕见）则重新接管。
                if proxy._orig is None:
                    proxy._orig = NSApp.delegate()
                NSApp.setDelegate_(proxy)
                self._reopen_proxy = proxy
            self._reopen_proxy._window = self
            log_line("reopen handler installed (forward delegate)")
        except Exception as exc:
            log_line("reopen handler install failed: %r" % (exc,))

    def _close_editor_safely(self):
        """关闭替换股票窗；底层对象已销毁时清掉残留引用，不可中断主窗动作。"""
        editor = getattr(self, "_editor", None)
        if editor is None:
            return
        try:
            if editor.isVisible():
                editor.close()
        except RuntimeError:
            self._editor = None
        except Exception as exc:
            log_line("editor close skipped: %r" % (exc,), "WARNING")

    # ---------- v1.4.40 人脸在场守卫 ----------
    # 分工：sf_presence 只数"画面里有几张脸"，本窗口决定"该不该收"。
    # 迟滞判定跑在 Qt 主线程的轮询表里，采集与 Vision 跑在后台线程，两边只共享
    # 一个 (计数, 时刻) 元组 —— 本仓有过"从 Qt 伸手进 AppKit"导致离屏 SIGSEGV 的
    # 事故（v1.4.33 收口），所以这条单向边界刻意保持住，不要在采集线程里碰窗口。
    def _face_guard_available(self):
        """kill switch：features.ini 写 face_guard=0 时菜单项、采集、恢复一并撤掉。"""
        return feature_enabled("face_guard")

    def _face_guard_protecting(self):
        """诊断包里"保护在不在跑"的口径：**摄像头此刻真的开着**，不是"配置里开没开"（v1.4.50）。

        为什么必须换掉口径（实机日志实证，台账 E02 复发；当时的产物是 ad-hoc 签名，现行为固定自签名）：
        每次更新后 TCC 把它当新程序，授权回到 undetermined。v1.4.47 按用户要求
        "授权框只在点开启时出现"，于是这一种现场成了死循环 ——
        配置说开着 → 勾选显示 ✓ → 用户以为在保护 → 点一下变成"关" → 再点一次才
        是"开"、才可能弹授权框。而状态行写的"再点一次菜单"指的还是屏幕上不存在
        的菜单项名字。第二个人入镜当然不会收窗：摄像头从头到尾没打开过。
        新口径把这两件事一起解决：没在保护就不打勾，一次点击即发起授权；配置里
        的开启偏好（`_face_guard_pref`）保持不变，重启后仍会继续重试。
        ⚠ v1.4.67 起菜单勾号**不再直接用本方法**：勾号与点击目标共用
        `_face_guard_switch_on()`（starting 也算开，理由见那边）。本方法保持
        "真在保护"的严格口径，给诊断包与状态行用。
        """
        if not self._face_guard_pref:
            return False
        mon = self._face_guard_monitor
        return bool(mon is not None
                    and getattr(mon, "state", "") == sf_presence.STATE_RUNNING)

    def _face_guard_switch_on(self):
        """菜单勾号与「点下去会切成什么」**共用**的唯一口径（v1.4.67）。

        口径：用户开了这个功能，且会话**正在起或已在跑**（starting/running）算"开"；
        needs_auth / denied / restricted / no_camera / failed / off 一律算"关"。

        为什么 starting 也算开（本轮修复的根因）：会话起起来要 0.2~0.6 秒（v1.4.54
        实测），首次授权要等系统框（本机日志实测 3.7 秒），而菜单勾号每 300ms 刷一次
        真值。旧口径（必须 running）在这个窗口里**每一拍都把用户刚点出来的勾按回去**
        —— 复现序列 [True×6, False×12]，用户看到的就是"第一次点击没反应，第二次才
        生效"（2026-10-12 报障）。starting 期间说"开"不是假话：采集会话此刻正在被
        拉起；起失败的状态会让勾号在一拍之内灭回去
        （tests/test_face_guard.py::test_tick_goes_back_off_when_the_start_fails 钉着）。
        needs_auth 不算开：系统框还摆在屏幕上等人作答，这时候说"开"是 E02 那一族的话。

        为什么点击目标也从这里取反，而不是 `not _face_guard_protecting()`：
        勾号亮着的时候，用户再点一下的唯一合理解读是"关"。目标与显示必须同一个
        口径，否则在启动窗口里点几下都是"开"——那正是报障的后半句。
        """
        if not self._face_guard_pref:
            return False
        mon = self._face_guard_monitor
        state = getattr(mon, "state", "") if mon is not None else ""
        return state in (sf_presence.STATE_STARTING, sf_presence.STATE_RUNNING)

    def face_guard_diagnostics(self):
        """人脸守卫的可观测状态。界面状态行与诊断包共用这一份，不存在两套口径。

        为什么必须能被外部读到：这是一个"没在跑也看不出区别"的功能。授权被拒、
        没摄像头、会话起不来时，若界面和诊断包都看不出来，用户就带着"以为有保护"
        的心态继续用 —— 比压根没这个功能更危险。有了这条，用户报"没生效"时不必
        让他复现，诊断包里直接有现场。
        """
        mon = self._face_guard_monitor
        a = self._face_guard_arbiter
        return {
            "available": self._face_guard_available(),
            "enabled_by_user": bool(self._face_guard_pref),
            "paused": bool(self._face_guard_paused),
            "stalled": bool(self._face_guard_stalled),
            "stale_sec": FACE_GUARD_STALE_SEC,
            # "摄像头还在我手里吗" —— 挂起未兑现的停也算还拿着（S1：以前这里说谎，
            # 于是"绿灯不灭"这件事在诊断包里看不出来）
            "camera_held": (mon is not None) or bool(self._face_guard_pending_stop),
            "pending_stops": len(self._face_guard_pending_stop),
            "state": (getattr(mon, "state", sf_presence.STATE_OFF)
                      if mon is not None else sf_presence.STATE_OFF),
            "reason": (getattr(mon, "last_error", "") or "") if mon is not None else "",
            "hidden_by_guard": bool(getattr(a, "hidden_by_guard", False)),
            "last_faces": getattr(a, "last_faces", None),
            "tick_sec": getattr(a, "tick_sec", sf_presence.FACE_GUARD_TICK_SEC),
            "hide_hits": getattr(a, "hide_hits", sf_presence.FACE_GUARD_HIDE_HITS),
            "restore_hits": getattr(a, "restore_hits", sf_presence.FACE_GUARD_RESTORE_HITS),
            "max_idle_sec": getattr(a, "max_idle_sec", sf_presence.FACE_GUARD_MAX_IDLE_SEC),
            "conf_min": getattr(mon, "conf_min", sf_presence.FACE_GUARD_CONF_MIN),
            # v1.4.63 边脸放宽与 1.5 米闸门：这三条一起决定"几张算数"，只写 conf_min
            # 就等于让外面按旧口径读这份诊断（贴边的框实际用的是 0.55，不是 0.6）。
            "edge_conf_min": sf_presence.FACE_GUARD_EDGE_CONF_MIN,
            "near_face_w": sf_presence.FACE_GUARD_NEAR_FACE_W,
            "edge_shrink": sf_presence.FACE_GUARD_EDGE_SHRINK,
            "dwell_sec": getattr(a, "dwell_sec", sf_presence.FACE_GUARD_DWELL_SEC),
            # 几何读不出来 = 贴边放宽与距离闸门双双失效（降级方向是保守的，但不能静默）。
            # 没有这个计数，"边缘的人不收窗"这类事只能靠复现，日志里一个字都没有。
            "geometry_failures": int(sf_presence.geometry_failures()),
            # v1.4.67 诊断补全：下面四项是"开着但没在工作"剩下的四个问不通的地方。
            # 系统侧原始授权态 —— state 归 off 时用它分清"用户从没点过允许(undetermined)"
            # "被拒(denied)""受限(restricted)""桥接不可用(unsupported)"。
            # 只读不弹窗（camera_access_status 的契约）；kill switch 关掉时不碰框架。
            # 只有真 .app 里 TCC 的答案才是"我们这个 app 的授权"：裸解释器问出来的是
            # python 自己的状态，报成 denied 就是假信号（见 has_bundle_identity）。
            "camera_access": ("no_bundle" if not has_bundle_identity()
                              else ("disabled" if not feature_enabled("face_guard")
                                    else sf_presence.camera_access_status())),
            # 轮询表活性：AGENTS §5.2 对"停掉一张回调表、又指望它自己醒"要求的判据，
            # 此前只在测试里有。false 而 hidden_by_guard=true = 一整天没有自动隐藏。
            "guard_timer_active": bool(self._guard_timer is not None
                                       and self._guard_timer.isActive()),
            # 最近一次停会话的原因与距今多久（state=off 之后唯一的"为什么停了"）。
            "last_stop": self._face_guard_stop_evidence(),
            # fail-open 之后**实际生效**的采集档与帧率（1.4.54：不支持时只写一行
            # WARNING，会被轮转挤掉；这里给"到底按哪档在跑"）。
            "applied_profile": (dict(getattr(mon, "applied_profile", {}))
                                if getattr(mon, "applied_profile", None) else {}),
            # 迟滞进度：连击计数与驻留起点距今多久。"客人坐了两秒才收"要看的是
            # 进度条走到哪，而不是只看阈值表。
            "hide_streak": int(getattr(a, "_hide_streak", 0) or 0),
            "restore_streak": int(getattr(a, "_restore_streak", 0) or 0),
            "two_since_age": self._face_guard_streak_age("two"),
            "one_since_age": self._face_guard_streak_age("one"),
            # 上一次喂进迟滞的快照距今多久（去重那一刻的证据；None = 还没喂过）
            "fed_age": (round(max(0.0, time.monotonic() - float(self._face_guard_fed_ts)), 2)
                        if float(self._face_guard_fed_ts or 0.0) > 0.0 else None),
            # 最近一帧里最大那张达标脸占画面宽的比例 —— 把"1.5 米"换成这个比例要靠
            # 镜头视角，而视角在本机读不到，所以留下这根标定棒（配合量出来的坐姿距离）。
            "max_face_width": round(float(
                getattr(mon, "last_face_width", 0.0) or 0.0), 4),
            # v1.4.50：这一段会话到底有没有帧、帧里有没有一次检测成功过。
            # 只有"运行中/未运行"两态时，"会话在跑但一帧都没数成"和"每秒都在数"
            # 在日志与诊断包里长得一模一样 —— 台账 E02 复发就是这么花了三轮才定位。
            "frames": int(getattr(mon, "frames", 0) or 0),
            "detect_errors": int(getattr(mon, "detect_errors", 0) or 0),
            "protecting": self._face_guard_protecting(),
            # v1.4.56：省电暂停与加急复检的现场（诊断包要能回答"为什么这段时间
            # 没在看守"——答案是"屏幕锁着"，而不是"功能坏了"）。
            "screen_off": bool(self._face_guard_screen_locked),
            "expedited": int(getattr(self._face_guard_monitor, "expedited", 0) or 0),
            # A2（v1.4.45）：RUNNING 却一帧都没来过时，距会话启动过了多久。
            # 状态行据此在"等画面"的窗口期说实话，判定据此提前判中断；其余情况恒 0。
            "zero_frame_sec": (
                max(0.0, time.monotonic()
                    - float(getattr(mon, "running_since", 0.0) or 0.0))
                if (mon is not None
                    and getattr(mon, "state", "") == sf_presence.STATE_RUNNING
                    and mon.snapshot()[1] <= 0.0) else 0.0),
        }

    def face_guard_field_evidence(self):
        """诊断包 `face_guard.json` 那一份：状态 + 那句人话（v1.4.51）。

        为什么单独一个方法而不把 `status_text` 塞进 `face_guard_diagnostics()`：
        那句话是**读**这份诊断算出来的（`face_guard_status_text(d)`），塞进去就成了
        诊断 → 文案 → 诊断 的无限递归（本轮实测踩过一次，57 条用例同时 RecursionError）。
        菜单不再展示提示语之后，这里就是那句话唯一的外发通路 —— 用户报"没生效"时
        仍然不必让他复现。
        """
        d = self.face_guard_diagnostics()
        d["status_text"] = self.face_guard_status_text(d)
        return d

    def face_guard_status_text(self, d=None):
        """把"保护到底在不在跑"和生效阈值说成一句人话。

        v1.4.51 起这句话**不再出现在右键菜单里**（用户看图要求"提示语去掉，不展示"），
        v1.4.52 起连首次开启那次说明框也撤了（同一位用户看图要求"去掉这个弹窗提示，
        永不出现"）。现在它只有两个去处：诊断包 `face_guard.json` 的 `status_text`、
        以及日志里的状态行。界面诚实由勾选本身承担（口径见
        `_face_guard_switch_on`：在保护、或刚点下正在起会话才勾；起不来的状态不勾）。

        `d` 传 `face_guard_diagnostics()` 的现成结果，避免自己调自己。
        阈值必须写进这句话，否则"为什么过了两秒才收"只能靠猜。
        """
        d = self.face_guard_diagnostics() if d is None else d
        if not d["available"]:
            return "人脸守卫：已由配置关闭"
        if not d["enabled_by_user"]:
            return "未开启 · 开启后需要允许摄像头权限"
        if d["stalled"]:
            # 复核 F2：一帧都不来的时候绝不能继续写"运行中"。用户看到的就是"开着但没用"，
            # 而真实情况是摄像头被别的程序占走、保护已经停了，等它松开会自动重试。
            return ("⚠ 保护已中断 · 最近 %d 秒采不到画面（摄像头可能被别的程序占用），"
                    "已停手等它松开" % int(d["stale_sec"]))
        if d["paused"]:
            return ("已暂停 · 收起后 %d 分钟内没人回到座位，摄像头已关；"
                    "再点一次「%s」或点 Dock 图标即可恢复"
                    % (int(d["max_idle_sec"] // 60), FACE_GUARD_MENU_TEXT))
        if d.get("screen_off"):
            return ("屏幕已锁定或显示器休眠 · 摄像头暂停省电（屏幕上没东西可看），"
                    "唤醒后自动继续看守")
        st = d["state"]
        if st == sf_presence.STATE_NEEDS_AUTH:
            # 用户自己开了守卫，但还没给权限：这一行必须同时说清"现在没在保护"和
            # "怎么让它开始保护"，否则就是"看起来开了其实没跑"（本仓反复防的那类）。
            # v1.4.50 改这两处文案的依据（实机日志取证，台账 E02 复发）：
            #   ① 旧文案指的「人脸在场守卫」在屏幕上不存在，真实菜单项是
            #      `FACE_GUARD_MENU_TEXT`（当前值见该常量），用户照着找找不到；
            #   ② 勾选口径改成"此刻在不在保护"之后，现在**一次**点击就发起授权请求；
            #      旧说法"再点一次"背后的两次点击（先关再开）已经不存在。
            return ("⚠ 未授权 · 保护没在跑（不弹系统框打扰你）；"
                    "点一次「%s」就会出现授权框" % FACE_GUARD_MENU_TEXT)
        if st == sf_presence.STATE_RUNNING:
            if d["detect_errors"]:
                # v1.4.50：帧来了却一次都没数成 —— 以前这一档和"运行中"共用一行，
                # 表现就是"绿灯亮、状态行写运行中、第二个人入镜什么都不发生"。
                return ("⚠ 画面已采到但系统认不出脸（已连续失败 %d 次），"
                        "第二人入镜不会收起 · 原因见日志 face_guard 检测失败"
                        % d["detect_errors"])
            if d["zero_frame_sec"] > 0.0:
                # A2（v1.4.45）：还没来过一帧的窗口期把实话说出来 —— 这一分钟里
                # 第二个人入镜并不会收窗，写"运行中"就是骗人在有保护。
                base = ("已启动 · 尚未收到画面（%d 秒内仍无画面将判定中断，"
                        "通常是摄像头被别的程序占用）" % int(FACE_GUARD_FIRST_FRAME_SEC))
                if d["hidden_by_guard"]:
                    return base + " · 已收起，等恢复后继续看"
                return base
            # 这句话只进诊断包与日志（v1.4.51/52 起界面不展示），所以宁可写全：
            # 2026-10-12 加了驻留、贴边放宽、距离闸门三条之后，只写"连续 N 次"
            # 就等于让外面按旧口径理解 —— 而这三条恰好是"人站我旁边为什么没收窗"
            # 唯一可能的答案所在。
            base = ("运行中 · 每 %g 秒数一次脸，连续 %d 次看到 2 张以上、"
                    "且第二张脸在半径内停留满 %g 秒才收起"
                    "（把握分门槛：画面中 %.2f / 贴边 %.2f；"
                    "距离闸门：脸宽需占画面 %.1f%% 以上，贴边按 %.2f 折算）"
                    % (d["tick_sec"], d["hide_hits"], d["dwell_sec"],
                       d["conf_min"], d["edge_conf_min"],
                       d["near_face_w"] * 100, d["edge_shrink"]))
            if d["hidden_by_guard"]:
                return base + " · 已收起，等你一个人回到座位（连续 %d 次）" % d["restore_hits"]
            return base
        if st == sf_presence.STATE_STARTING:
            return "等待摄像头授权 · 请在系统弹窗里点「允许」"
        if st == sf_presence.STATE_DENIED:
            return "⚠ 保护未生效 · 摄像头权限被拒绝（系统设置 › 隐私与安全性 › 摄像头）"
        if st == sf_presence.STATE_RESTRICTED:
            return "⚠ 保护未生效 · 摄像头被系统策略限制（描述文件/家长控制）"
        if st == sf_presence.STATE_NO_CAMERA:
            return "⚠ 保护未生效 · 没有可用摄像头"
        return "⚠ 保护未生效 · " + (d["reason"] or "未知原因")

    def _on_face_guard_toggled(self, checked, action=None):
        """菜单勾选 → 写配置 → 立刻按新状态同步采集。"""
        checked = bool(checked)
        prev = bool(self._face_guard_pref)
        if not save_face_guard_pref(checked):
            # 写盘失败必须看得见：否则用户以为开了，实际下次启动又回到关。
            self._show_update_message(
                "设置没能保存",
                "人脸守卫现在按%s运行，但重启后会回到%s。"
                % ("开启" if checked else "关闭", "开启" if prev else "关闭"),
                "配置文件写入失败，请检查磁盘剩余空间与 ~/Library/Application Support 的写入权限。")
            if action is not None:
                try:
                    action.blockSignals(True)
                    action.setChecked(prev)
                    action.blockSignals(False)
                except Exception as exc:
                    log_line("face_guard menu revert failed: %r" % (exc,), "WARNING")
            return
        self._face_guard_pref = checked
        self._face_guard_paused = False
        # 用户亲手动了开关 = 旧的"采不到画面"判定作废，重新按现状走一遍
        self._face_guard_stalled = False
        self._face_guard_last_fresh = 0.0
        if checked:
            # 授权请求只在**这一刻**发起（v1.4.47 起，用户要求去掉开机就弹的系统框）：
            # monitor 自己不再在启动时 requestAccess，所以这里显式问一次。
            # 先拿现成的 monitor（没有就建一个），请求完再交给 _sync_face_guard 起会话。
            # v1.4.52：这里**不再弹**任何"代价说明"框（用户 2026-10-09 看图要求
            # "去掉这个弹窗提示，永不出现"）。开启路径上唯一会出现的框是系统的
            # 摄像头授权框 —— 那是 macOS 的，不是我们的。
            if self._face_guard_monitor is None:
                self._face_guard_monitor = self._face_guard_new_monitor()
            outcome = self._face_guard_monitor.request_access_once()
            log_line("face_guard 授权发起点=菜单点击，结果=%s" % outcome)
            if outcome in ("denied", "restricted"):
                # 权限被**明确拒绝**过之后，macOS 永远不会再弹授权框 —— 那一下点击
                # 于是又变成"没反应"（v1.4.57 修的是"没答"，这是另一半"答了不允许"）。
                # 与其让菜单空转，不如把那一下直接带到系统设置的摄像头页：
                # 这不是解释性弹窗（本 app 不加），而是完成这件事唯一剩下的步骤。
                self._open_camera_privacy_settings()
        else:
            log_line("face_guard 关闭")
        self._sync_face_guard("menu", stop_wait=False)

    CAMERA_PRIVACY_URL = ("x-apple.systempreferences:com.apple.preference.security"
                          "?Privacy_Camera")

    def _open_camera_privacy_settings(self):
        """打开系统的「隐私与安全性 → 摄像头」面板。失败只留痕，绝不打断菜单操作。

        用 NSWorkspace 而不是 `open` 子进程：本 app 已经在用 PyObjC，起子进程
        反而多一条会失败的路径。装不上 pyobjc 的环境（裸 python 跑测试）里
        这里必然抛，所以整段兜住并记日志 —— 但**不许静默**。
        """
        try:
            from AppKit import NSWorkspace
            from Foundation import NSURL
            url = NSURL.URLWithString_(self.CAMERA_PRIVACY_URL)
            if url is None:
                log_line("face_guard 打不开摄像头设置页：URL 解析失败", "WARNING")
                return False
            ok = bool(NSWorkspace.sharedWorkspace().openURL_(url))
            log_line("face_guard 权限被拒，已打开系统摄像头设置页 ok=%s" % ok)
            return ok
        except Exception as exc:
            log_line("face_guard open camera settings failed: %r" % (exc,), "WARNING")
            return False

    def _face_guard_new_monitor(self):
        """建 monitor，并把"数完就判"的回调挂上（2026-10-12 事件驱动那一格）。

        回调只做一件事：emit 一个排队信号。它跑在**采集线程**里，所以绝不允许
        在这里读 QWidget、拼字符串或写日志 —— 出问题时会带走整帧计数。
        emit 失败（窗口正在销毁等）由 sf_presence 那侧 log_swallow 留痕。
        """
        return sf_presence.FacePresenceMonitor(
            on_sample=self.face_guard_wakeup.emit)

    def _sync_face_guard(self, reason="", stop_wait=True):
        return FaceGuardController.sync(
            _lazy_attr(self, "_face_guard_controller", FaceGuardController), self, reason, stop_wait)

    def _stop_face_guard(self, reason="", wait=True, stop_timer=True):
        return FaceGuardController.stop(
            _lazy_attr(self, "_face_guard_controller", FaceGuardController), self, reason, wait, stop_timer)

    def _drain_pending_guard_stops(self, reason=""):
        return FaceGuardController.drain_pending(
            _lazy_attr(self, "_face_guard_controller", FaceGuardController), self, reason)

    def _on_face_guard_tick(self):
        """守卫的判定入口：采集线程数完新样本立刻进来一次，另有同拍看门狗兜底。

        两个身份共用一个函数（2026-10-12 起）：
          · 事件驱动 —— `face_guard_wakeup` 排队信号，数到新快照就判，不等表；
          · 看门狗   —— `_guard_timer`（与采样同拍），专门兜"信号没送达"那一种。
        两条都进来也不怕重复：喂给迟滞之前按样本时刻去重，同一份快照只算一次，
        所以"连续两次命中"是真的两次独立复检，驻留计时也是真的看了多久。
        """
        if not self._face_guard_available() or not self._face_guard_pref:
            self._stop_face_guard("pref off")
            return
        mon = self._face_guard_monitor
        a = self._face_guard_arbiter
        now = time.monotonic()
        # v1.4.56：屏幕锁着/显示器休眠 = 屏幕上没有任何可暴露的内容，摄像头先停。
        # 只在系统**明确回答**「锁了/睡了」时才停（见 sf_presence.screen_unavailable
        # 的 fail-open 方向）；停表后把下一次重试排在探测节流之后，唤醒即恢复。
        if self._face_guard_screen_off(now):
            if mon is not None:
                # 停摄像头，但**留着轮询表**：解锁之后就是这张表把看守重新起来的。
                self._stop_face_guard("screen off", wait=False, stop_timer=False)
            self._face_guard_next_retry = now + FACE_GUARD_SCREEN_PROBE_SEC
            return
        if mon is None or a is None:
            # 被停过（含"采不到画面"的中断自愈）就按退避节拍重试，不能每半秒抢一次摄像头。
            if now < self._face_guard_next_retry:
                return
            self._face_guard_next_retry = now + 5.0
            self._sync_face_guard("tick", stop_wait=False)
            return
        if mon.state != sf_presence.STATE_RUNNING:
            # 没起来（还在等授权 / 上一次失败）：后台线程正在拉起时不打扰它。
            if mon.is_starting():
                return
            if now < self._face_guard_next_retry:
                return
            # 还没授权/刚建对象：2 秒试一次，用户点了「允许」之后很快就能起来。
            # 明确被拒、没摄像头、框架不可用这几类**只能由用户去系统设置里改**，
            # 10 秒一次就够 —— 半秒一次既没有意义，又把日志和线程搅出一片空转。
            stalled_wait = (FACE_GUARD_RETRY_STALLED_SEC
                            if self._face_guard_stalled else 0.0)
            early = mon.state in (sf_presence.STATE_STARTING, sf_presence.STATE_OFF,
                                  sf_presence.STATE_NEEDS_AUTH)
            self._face_guard_next_retry = now + max(
                stalled_wait, 2.0 if early else 10.0)
            self._sync_face_guard("retry:%s" % mon.state, stop_wait=False)
            return
        # 复核 F3：窗口以"缩小"以外的路径下屏时（系统隐藏应用、Space 切换等），
        # 没有任何人会再调一次 _sync_face_guard，摄像头就会一直开着。每拍做一次
        # 廉价复核，把"只在窗口可见时检测"这条用户选的口径真正兜住。
        if not self.isVisible() and not a.hidden_by_guard:
            self._sync_face_guard("visibility drift", stop_wait=False)
            return
        count, ts = mon.snapshot()
        # 复核 F2：一帧都没收到时（ts=0）不能"先不管"。实测 `startRunning` 在设备被
        # 别的程序占用、或权限还没给的情况下**照样返回在跑却一帧不发**，那种形态下
        # 只看最后一帧的时刻会永远等下去 —— 绿灯常亮、状态行写着"运行中"、看守超时
        # 也不推进，正是 F2 要修的症状本身。零帧时改用"会话进入 RUNNING 的时刻"当基线。
        baseline = ts if ts > 0.0 else float(getattr(mon, "running_since", 0.0) or 0.0)
        if baseline <= 0.0:
            return
        if ts <= 0.0 and now - baseline >= FACE_GUARD_FIRST_FRAME_SEC:
            # A2（v1.4.45）："从来没来过帧"不用等满 60 秒 —— startRunning 之后 10 秒
            # 还一帧没有，基本就是设备被占用或通道坏了，拖满 60 秒只是让绿灯白亮
            # 一分钟、期间看守完全不工作。60 秒那条继续留给"来过帧、后来停了"的会话。
            self._face_guard_stalled = True
            log_line("face_guard 判定中断：启动 %.0f 秒从未收到画面，停摄像头等自愈"
                     % FACE_GUARD_FIRST_FRAME_SEC, "WARNING")
            self._stop_face_guard("stalled", wait=False)
            try:
                # 停表顺手把轮询也停了；自愈全靠这张表，这里重新挂上。
                self._guard_timer.start()
            except Exception as exc:
                log_swallow("stock_float_window._on_face_guard_tick", exc)
            self._face_guard_next_retry = now + FACE_GUARD_RETRY_STALLED_SEC
            return
        if now - baseline > max(4 * mon.tick_sec, 4.0):
            # 超过 4 个节拍没有新帧：多半是摄像头被别的进程抢走。此时**不喂旧样本** ——
            # 拿一分钟前的"一个人"去触发恢复，等于用过期现场做即时决策。
            log_line_throttled("face_guard_stale",
                               "face_guard 采不到新帧，暂停判定（摄像头可能被占用）")
            if self._face_guard_last_fresh <= 0.0:
                self._face_guard_last_fresh = baseline
            if (now - self._face_guard_last_fresh
                    >= FACE_GUARD_STALE_SEC):
                # 复核 F2：光"这一拍不判定"是不够的 —— 看守超时靠喂入推进，一帧不来
                # 就永远不推进，于是绿灯常亮、状态行还写着"运行中"。现在明确判定
                # "保护已中断"：停摄像头、状态行如实写，并按 30 秒退避重试等镜头松开。
                self._face_guard_stalled = True
                log_line("face_guard 判定中断：连续 %.0f 秒采不到画面，停摄像头等自愈"
                         % FACE_GUARD_STALE_SEC, "WARNING")
                self._stop_face_guard("stalled", wait=False)
                try:
                    # 停表顺手把轮询也停了；自愈全靠这张表，这里重新挂上。
                    self._guard_timer.start()
                except Exception as exc:
                    log_swallow("stock_float_window._on_face_guard_tick", exc)
                self._face_guard_next_retry = (
                    now + FACE_GUARD_RETRY_STALLED_SEC)
            return
        self._face_guard_last_fresh = now
        self._face_guard_stalled = False
        # **同一份快照只喂一次**（2026-10-12，二次复核 F1 的另一半）。
        # 以前主线程问得比采集快，一份"两张脸"的快照会被连读两次，于是
        # "连续 2 次命中"实际只等于"一次视觉计数"—— 迟滞想防的单帧误检并没有真防住，
        # 而驻留时长（>0.38 秒）也会被这种重复喂入撑成假数字。
        # 去重之后：两次命中 = 两次独立复检，驻留计时也才是"真的看了多久"。
        if ts == self._face_guard_fed_ts:
            return
        self._face_guard_fed_ts = ts
        event = a.feed(count)
        if event == sf_presence.EVENT_HIDE:
            log_line("face_guard 收起：连续命中，画面 %d 张脸" % count)
            # 更新流程的弹窗**刻意不在这里收掉**：它由 _close_update_flow_box 在退出
            # 与失败路径里统一收口，装更新装到一半被强制关掉会让收尾踩空。
            # 也就是说：自动收起时若恰好有更新弹窗在屏上，那块文字不会跟着消失 —— 已知限制。
            # 第二张脸出现是隐私场景，窗口必须立刻消失，不能让缩小动画多留 0.3 秒
            self._instant_minimize = True
            try:
                self.minimize_to_dock()
            finally:
                self._instant_minimize = False
            # 收窗之后的"继续看守"由 minimize_to_dock 末尾的 _sync_face_guard 统一决定，
            # 这里不再重复调一次（重复调只是多写一条状态日志，没有别的收益）。
        elif event == sf_presence.EVENT_RESTORE:
            log_line("face_guard 恢复：连续 %d 次只剩一张脸" % a.restore_hits)
            self._face_guard_paused = False
            # 先清状态再恢复窗口：hidden_by_guard 若还留着，同步逻辑会认为
            # "仍在因第二张脸收起"而继续耗电，而实际画面里已经只剩主人了。
            a.reset()
            self._restore_window()       # 末尾的 _sync_face_guard 负责让采集与可见性对齐
        elif event == sf_presence.EVENT_IDLE:
            log_line("face_guard 看守超时，停摄像头改手动恢复", "WARNING")
            self._face_guard_paused = True
            self._sync_face_guard("idle timeout", stop_wait=False)

    # ---------- 收起 / 恢复的过渡动画（sf_dock_motion，用户 2026-10-23 要求） ----------
    # 类上给默认值：绕过构造的测试替身也读得到。动画只是装饰，真实窗口的收起/恢复
    # 状态不依赖它；任何一步拿不到（离屏、功能关、截图失败、系统开了"减少动态效果"）
    # 都退回原来的瞬间收起与恢复。
    _dock_motion_ctl = None
    _instant_minimize = False     # 人脸守卫的自动收起为了隐私必须立刻消失，置位期间不放动画

    def _dock_motion(self):
        """动画控制器；功能关闭或不在 cocoa 下返回 None。"""
        if not native_cocoa_active() or not feature_enabled("dock_motion"):
            return None
        ctl = self._dock_motion_ctl
        if ctl is None:
            ctl = self._dock_motion_ctl = sf_dock_motion.DockMotion()
        return ctl

    def _dock_motion_geometry(self):
        return (self.x(), self.y(), self.width(), self.height())

    def _dock_motion_numbers(self, nsw):
        """截图要合成的窗口号：玻璃层在下、内容窗在上。"""
        glass = getattr(self, "_glass_window", None)
        numbers = [int(glass.windowNumber())] if glass is not None else []
        numbers.append(int(nsw.windowNumber()))
        return numbers

    def _dock_motion_prewarm(self):
        """启动后在后台做一次截图，把首次调用的初始化成本提前花掉，第一次点缩小才不会迟钝。"""
        ctl = self._dock_motion()
        if ctl is None:
            return
        try:
            nsw = content_ns_window(self)
            if nsw is not None:
                ctl.prewarm(self._dock_motion_numbers(nsw))
        except Exception as exc:
            log_swallow("stock_float_window._dock_motion_prewarm", exc)

    def _dock_motion_collapse(self, ctl):
        """收起前截图；非自动收起时再让代理窗盖住真实窗口。必须在真实窗口隐藏之前调用。"""
        try:
            nsw = content_ns_window(self)
            if nsw is None or not self.isVisible():
                return False
            numbers = self._dock_motion_numbers(nsw)
            rect = self._dock_motion_geometry()
            if not ctl.capture(numbers, rect, int(nsw.level()), int(nsw.collectionBehavior())):
                return False
            if self._instant_minimize:
                return False
            return ctl.collapse(ctl.icon_rect(rect, {BUNDLE_ID, *LEGACY_BUNDLE_IDS}))
        except Exception as exc:
            log_swallow("stock_float_window._dock_motion_collapse", exc)
            return False

    def _dock_motion_expand(self):
        """朝窗口原位放大；返回 True 表示动画已接手（真实窗口等动画结束才显示）。"""
        ctl = self._dock_motion()
        if ctl is None:
            return False
        try:
            if not self._minimized or getattr(self, "_quitting", False) or not getattr(self, "running", True):
                return False         # 交给 _restore_window_now 里原有的守卫去记日志
            if ctl.busy:
                if not ctl.collapsing:
                    return True      # 已经在放大，重复触发吞掉
                ctl.cancel()         # 缩小到一半又被点开：收掉缩小，改为从图标处放大
            if not ctl.has_snapshot:
                return False
            rect = ctl.icon_rect(self._dock_motion_geometry(), {BUNDLE_ID, *LEGACY_BUNDLE_IDS})
            return ctl.expand(rect, self._restore_window_now)
        except Exception as exc:
            log_swallow("stock_float_window._dock_motion_expand", exc)
            return False

    def minimize_to_dock(self):
        """缩小悬浮窗到 Dock 程序坞：隐藏窗口，Dock 显示应用图标"""
        log_line("minimize_to_dock called")
        ctl = self._dock_motion()
        if ctl is not None:
            ctl.cancel()             # 放大到一半又要收起：先收掉在飞的代理窗
        # 编辑器可能已被 Qt 销毁而 Python 引用尚存；任何残留都不能阻断缩小主窗。
        self._close_editor_safely()
        # v1.2.87 补丁（M3）：目标价面板同样必须收口。原实现只关编辑器，
        # 面板会孤零零悬浮在屏幕上，且仍能输入价格并「立即生效」写回已隐藏的
        # 主窗 —— 用户从 Dock 恢复后还看到面板、编辑图标仍是置灰的。
        try:
            self._close_target_panel()
        except Exception:
            pass
        # v1.3.42 第1轮走查：气泡与按钮交互态必须与主窗一起收口。
        # 原实现只关编辑器与目标价面板，漏了两样：
        #   ① 目标价气泡（TargetBubble）是**独立 NSWindow**，主窗 hide() 它照旧
        #      悬浮在屏幕原地 —— 缩小到 Dock 后桌面上还剩一个孤零零的气泡；
        #   ② _hover_btn / _press_btn 未复位，从 Dock 恢复后按钮仍是"按下"的灰底板。
        try:
            self._hide_target_bubble()
        except Exception:
            pass
        try:
            self._hover_btn = None
            self._press_btn = None
            self._press_btn_actioned = False
        except Exception:
            pass
        # 取消恢复时延迟隐藏 Dock 图标的 pending 任务，避免竞态
        if self._hide_dock_timer is not None:
            self._hide_dock_timer.stop()
            self._hide_dock_timer = None
        # 截图与代理窗上屏要排在真实窗口隐藏之前，否则中间会闪一帧空桌面
        if ctl is not None:
            self._dock_motion_collapse(ctl)
        # v1.2.41：缩 Dock 门控下沉 sf_runtime（enter_dock 置 _minimized=True，随后的
        # _update_nap_policy(False) 走 RuntimeState.update_nap 停用 App Nap 允许省电）
        self._runtime.enter_dock()
        self.hide()
        # v1.4.10：缩 Dock 会把轮询间隔抬到 POLL_INTERVAL_MINIMIZED(2s)，
        # 不叫醒的话线程仍按旧的短睡节拍跑完才重算（行为不致错，只是白醒一次）。
        self._resched_loops()
        # 独立毛玻璃窗需同步隐藏，否则缩小后玻璃层仍悬浮在屏幕原地
        gw = getattr(self, "_glass_window", None)
        if gw is not None:
            try:
                gw.orderOut_(None)
            except Exception:
                pass
        self.top_timer.stop()
        self._sync_dot_timer()          # v1.4.14：没有观众，呼吸灯时钟即刻停表
        # v1.2.39：停止持续禁止 App Nap（允许系统省电），停跌幅闪烁，整体降频
        self._update_nap_policy(False)
        try:
            self._flash_drop_timer.stop()
        except Exception:
            pass
        if self.timer.interval() != int(POLL_INTERVAL_MINIMIZED * 1000):
            self.timer.setInterval(int(POLL_INTERVAL_MINIMIZED * 1000))
        show_dock()
        # v1.4.40 人脸守卫：窗口一收，摄像头该不该继续开着，取决于"是谁收的"——
        # 被第二张脸逼着收起的必须继续看着，否则"只剩一张脸就自动恢复"没有数据来源；
        # 用户自己点缩小的直接停（绿灯灭、不耗电），恢复只由人点 Dock 图标驱动。
        # v1.4.41 复核 F5：这条是交互路径，stop_wait=False —— 万一后台正在首次拉起
        # 会话（实测占数百毫秒），主线程等在那儿就是"点一下缩小，界面卡半秒"。
        self._sync_face_guard("minimize", stop_wait=False)

    def _restore_window(self):
        """恢复悬浮窗：先尝试从 Dock 图标放大的过渡动画，接不了手就立刻恢复。

        动画接手时真实窗口要等放大结束才显示（`_restore_window_now` 作为结束回调）。
        """
        if self._dock_motion_expand():
            return
        self._restore_window_now()

    def _restore_window_now(self):
        """在 Qt 事件循环中恢复悬浮窗显示（Dock 图标常驻）。

        只允许从“已缩到 Dock”的状态恢复，避免普通应用激活、旧的 Dock 回调或
        原生按钮事件尾声触发重复 show/hide，造成主界面刚出现又消失。
        """
        if not self._minimized:
            log_line("restore_window ignored: not minimized", "INFO")
            return
        # v1.4.18 走查（S4）：quit() 有 pending 搜索线程时最多延后约 2s 才真退出，
        # 这窗口里若点击通知/旧的 AppKit reopen 回调抵达 _restore_window，会把已关闭
        # 的主窗重新 show 成僵尸浮窗（且期间 top_timer 已停）。退出中一律拒绝恢复。
        if getattr(self, "_quitting", False) or not getattr(self, "running", True):
            log_line("restore_window ignored: quitting/not running", "INFO")
            return
        # v1.2.41：恢复门控下沉 sf_runtime（exit_dock 清 _minimized，恢复后的
        # _update_nap_policy(交易态) 负责重新按交易状态启用 App Nap）
        self._runtime.exit_dock()
        self.show()
        self.raise_()
        self.activateWindow()
        # 恢复独立毛玻璃窗（缩小期间被隐藏，需重新显示）
        gw = getattr(self, "_glass_window", None)
        if gw is not None:
            try:
                gw.orderFront_(None)
            except Exception as _swallow_exc:
                log_swallow("stock_float_window._restore_window", _swallow_exc)
        # v1.4.26：这里必须**强制**重做一次完整置顶，不能被稳态体检跳过。
        # 主窗与玻璃窗同为 NSStatusWindowLevel(25)，谁压住谁只取决于最后一次
        # orderFront；上面刚把玻璃层提到最前，若置顶复核因为"层级/可见/frame/
        # Space 都对"而直接 return，玻璃就永久盖住 Qt 内容 —— 用户看到的正是
        # "只剩一块深色圆角背景、行情与按钮全不见"的空壳，而玻璃层
        # ignoresMouseEvents，点击穿透到下面的主窗，所以按原位置盲点关闭按钮
        # 仍然能退出程序。浏览器全屏期间恢复窗口同样命中这条路径（2026-09-23 现场）。
        # v1.4.27：走不带形参的 _reassert_top()。它跳过稳态体检、重做一次完整置顶，
        # 但**不给 _force_top 传参** —— 测试里大量 `lambda self: None` 的替身/打桩
        # （含公共夹具与功耗底座）会撞上 TypeError，而这一段没有异常兜底，恢复流程
        # 会当场断在这里。
        self._reassert_top()
        self.top_timer.start()
        self._sync_dot_timer()          # v1.4.14：恢复可见，呼吸灯立刻回到 60 帧
        # v1.2.39：恢复高频轮询 + 重新按交易状态启用 App Nap + 立即刷新一次
        self._update_nap_policy(self._is_trading_time())
        # v1.2.41：恢复窗口轮询间隔决策下沉 sf_runtime.restore_ui_interval（50/1000ms）
        target = restore_ui_interval(self._is_trading_time())
        if self.timer.interval() != target:
            self.timer.setInterval(target)
        QTimer.singleShot(0, self._poll)
        self._force_fetch()
        # v1.4.40：窗口重新可见 → 人脸守卫该开就开（被守卫收起时也在跑，这里幂等）；
        # 同时清掉"看守超时暂停"，用户主动点 Dock 就等于重新武装。
        self._face_guard_paused = False
        # v1.4.41 复核 F1：走到这里窗口已经显示，"因第二张脸被收起"这件事在事实上
        # 结束了，必须把迟滞状态机一起结掉。否则残留的 hidden_by_guard 会让下一次
        # 手动缩小不停摄像头，约 5 秒后程序还会自己把窗口弹回来推翻用户的操作。
        # 自动恢复那条路径先前已经 reset 过一次，重复 reset 无害。
        if self._face_guard_arbiter is not None:
            self._face_guard_arbiter.reset()
        self._sync_face_guard("restore", stop_wait=False)
        # v1.4.59（S2 第一处）：恢复之后必须钳一次屏。落点是缩小前写盘的旧坐标，
        # 而"缩在 Dock 期间拔掉外接屏 / 改了分辨率"这条序列里没有任何东西会替我们修它：
        # `_on_screen_changed` 开头就是 `if not self.isVisible() or self._minimized: return`，
        # 隐藏期间一律早退。于是点 Dock 之后窗口落在新屏幕之外 —— 桌面上什么都不出现，
        # 用户只能强退（这是本文件里"能恢复但看不见"的最坏形态）。
        self._clamp_to_screen(force=True)
        self._keep_on_screen()
        log_line("restore_window: shown, visible now=%r at=(%d,%d)"
                 % (self.isVisible(), self.x(), self.y()))

    def _on_app_state(self, state):
        """应用激活仅作状态观察；Dock 点击恢复由 AppKit reopen 回调驱动。

        其他恢复入口会直接调用 `_restore_window`。

        show_dock()/activateWindow() 本身就会触发 ApplicationActive，若这里再次尝试
        恢复，会制造无效回调风暴并与缩小、退出争夺窗口生命周期。
        """
        return

    def _persist_on_exit(self):
        """退出收尾的**轻量安全子集**：停定时器 + 持久化窗口位置/高度 + 回收 App Nap 令牌。

        v1.2.65 第1轮走查修复：此前**只有**点关闭按钮走 quit() 才会写盘配置。
        用 Cmd+Q、Dock 图标「退出」、或系统侧关闭窗口时，Python 侧收不到任何通知，
        窗口位置与拉伸高度全部丢失 —— 下次启动窗口回到上次「点过关闭按钮」时的
        位置，用户手动摆好的位置与调好的高度每次都要重来一遍。

        为什么这里不直接调 quit()：quit() 会 join 后台线程（各 1.5s 超时）、关
        网络连接池、关编辑器与毛玻璃窗，属于「主动退出」的重路径；而 quit() 末尾
        自己就有 self.close()，closeEvent 会在 quit() 的内部流程里被再次触发 ——
        直接调 quit() 既会重入，又会把每次退出拖慢数秒。这里只保留「不写盘就丢」
        的最小集合，并保证幂等（ quit() 也会走本方法，不重复写盘 ）。
        """
        if getattr(self, "_exit_persisted", False):
            return
        self._exit_persisted = True
        feedback = getattr(self, "_feedback_dialog", None)
        if feedback is not None:
            feedback.close()
        self._stock_transition.stop()
        try:
            self.running = False
        except Exception:
            pass
        # 必须先同步收口更新对话框，再让主窗 close()/QApplication.quit()。后台回包
        # 即使随后抵达，也只会看到 None，不会在 Qt 的析构期更新或销毁原生窗口。
        try:
            self._close_update_flow_box()
        except Exception as exc:
            log_swallow("stock_float_window._persist_on_exit.update_dialog", exc)
        try:
            motion = getattr(self, "_dock_motion_ctl", None)
            if motion is not None:
                motion.cancel()      # 它的定时器不是主窗的子对象，下面的兜底遍历收不到
        except Exception as exc:
            log_swallow("stock_float_window._persist_on_exit.dock_motion", exc)
        for t in (getattr(self, "timer", None), getattr(self, "top_timer", None),
                  getattr(self, "_anim_timer", None),
                  getattr(self, "_flash_timer", None),
                  getattr(self, "_flash_drop_timer", None),
                  getattr(self, "_rainbow_timer", None),
                  getattr(self, "_hide_dock_timer", None),
                  getattr(self, "_clear_timer", None)):
            if t is None:
                continue
            try:
                t.stop()
            except Exception:
                pass
        # v1.4.18 走查（S2）：手工枚举的定时器清单必然漏掉后续新增的表 —— v1.4.14 的
        # _dot_timer（16ms/60Hz 呼吸灯）与 _session_timer 就没在列。退出收尾若不停它们，
        # 窗口关闭到进程退出这段里 _dot_tick 仍会以 60Hz 调 self.update()。改为兜底遍历
        # 所有子 QTimer 一律停表，杜绝"新增定时器忘记登记"这类反复出现的收尾缺陷。
        try:
            for t in self.findChildren(QTimer):
                try:
                    t.stop()
                except Exception:
                    pass
        except Exception:
            pass
        try:
            r = save_config(self.code, self.x(), self.y(), self.height(), self._bg_cfg)
            if not r:
                log_line("exit: config save failed (check disk/permission)", "WARNING")
        except Exception as exc:
            log_line("exit persist error: %r" % (exc,))
        # Persist first; a stalled camera driver must not hold user data hostage.
        try:
            self._stop_face_guard("exit")
        except Exception as exc:
            log_swallow("stock_float_window._persist_on_exit.face_guard", exc)
        # v1.2.65：App Nap 令牌此前从未在退出时被回收（RuntimeState.shutdown 是
        # 死代码，全项目零调用）。进程退出时系统会回收，但异常路径/多实例场景
        # 下残留的电源断言会让机器无法睡。
        try:
            self._runtime.shutdown()
        except Exception:
            pass
        # v1.2.72 第6轮走查：系统关闭窗口会走 closeEvent → 本轻量收尾，但
        # 不会走 quit()；独立毛玻璃 NSWindow 因而残留在桌面形成一层不可交互
        # 的模糊背景。把它纳入所有退出路径共用的幂等收尾。
        gw = getattr(self, "_glass_window", None)
        if gw is not None:
            try:
                # v1.2.95 第7轮走查：绝不能对原生 NSWindow 调 close() —— AppKit
                # 释放后再被 Python 包装对象 release 即 EXC_BAD_ACCESS（本项目已
                # 4 次此类 objc_release SIGSEGV）。只 orderOut 下屏，保留 Python
                # 强引用由实例销毁时自然回收（与 sf_glass._close_glass 同构）。
                gw.orderOut_(None)
            except Exception:
                pass
            self._glass_window = None
        # v1.2.95：_bg_effect 此前只在注入失败/成功两处置位，所有退出路径从不
        # 复位 —— 收尾后 paintEvent 仍按"毛玻璃已生效"走玻璃分支绘制。
        self._bg_effect = False

    def keyPressEvent(self, e):
        """键盘快捷操作。

        Cmd+W：缩小悬浮窗到 Dock 程序坞 —— 与顶栏缩小按钮走同一入口
        （_trigger_top_button("min") → minimize_to_dock），行为完全一致。
        macOS 上 Command 键在 Qt 中即 ControlModifier。

        编辑器、目标价面板等子窗口获得焦点时，键盘事件先到达子窗口、
        不会进到这里，因此编辑/输入中途按 Cmd+W 不会误把主窗缩掉；
        此时由子窗口自己决定是否关闭。
        """
        if (e.key() == Qt.Key.Key_W
                and bool(e.modifiers() & Qt.KeyboardModifier.ControlModifier)):
            e.accept()
            self._trigger_top_button("min")
            return
        super().keyPressEvent(e)

    def closeEvent(self, e):
        """主窗被系统关闭时执行与关闭按钮一致的完整退出，不留下后台进程。"""
        if not getattr(self, "_quitting", False):
            try:
                self.quit()
            except Exception as exc:
                log_line("closeEvent quit error: %r" % (exc,))
                self._shutdown_threads()
                self._persist_on_exit()
                # 兜底退出路径同样不能把待删对象留给 exit() 的静态析构阶段。
                _flush_deferred_deletions()
                QApplication.quit()
        try:
            e.accept()
        except Exception:
            pass

    def _shutdown_threads(self):
        """退出时统一停止后台线程与连接池 —— 全项目唯一实现。

        v1.2.95 第7轮走查修复：Cmd+Q 走 QApplication::quit()、Dock 图标「退出」
        走 app.exit(0)，两者**都不**经过本窗的 quit()，也不投递 closeEvent ——
        此前线程清理只写在 quit() 里，于是：
          · quote-loop / minute-loop（daemon，常驻）与其 recover 重建线程没人置
            running=False、没人 _wake.set()、没人 join，只能靠进程退出硬杀；
          · sf_editor.SearchThread（QThread）完全没有任何停止机制，退出时可能
            "QThread: Destroyed while thread is still running" 甚至 abort。
        aboutToQuit 是这两条路径唯一的通知点，故线程清理必须下沉到本方法并由
        aboutToQuit 与 quit() 共用（幂等，重复调用直接返回）。
        """
        controller = getattr(self, "_update_controller", None)
        performance = getattr(self, "_performance_monitor", None)
        if performance is not None:
            performance.stop()
        if controller is not None:
            controller.stop()
        if getattr(self, "_threads_stopped", False):
            return
        self._threads_stopped = True
        maintenance = getattr(self, "_update_maintenance", None)
        if maintenance is not None:
            maintenance.stop()
        writer = getattr(self, "_snapshot_writer", None)
        if writer is not None and not writer.close():
            log_line("first-screen cache writer still finishing during exit", "WARNING")
        # QApplication 比窗口活得久；主动断开本窗口新增的恢复检查槽，避免测试或
        # 关闭后的临时窗口仍被 applicationStateChanged 持有并在退出阶段回调。
        try:
            app = QApplication.instance()
            callback = getattr(self, "_check_updates_after_resume", None)
            if app is not None and callback is not None:
                app.applicationStateChanged.disconnect(callback)
        except (TypeError, RuntimeError):
            pass
        # 停止更新调度，避免退出后仍触发静默检查或退避重试。
        for timer in (getattr(self, "_update_retry_timer", None),
                      getattr(self, "_update_periodic_timer", None)):
            if timer is not None:
                try:
                    timer.stop()
                except Exception:
                    pass
        # 更新下载同样受退出生命周期约束：尽快停止后续 Range 请求和临时文件写入。
        update_cancel = getattr(self, "_update_cancel", None)
        if update_cancel is not None:
            try:
                update_cancel.set()
            except Exception:
                pass
        # 置停止标志：后台循环 while self.running 的下一节拍即退出
        self.running = False
        # 唤醒所有可能在 _sleep/Event.wait 上长睡的线程，消除残余等待
        editor_wake = None
        try:
            editor = getattr(self, "_editor", None)
            if editor is not None:
                editor_wake = getattr(editor, "_wake", None)
        except RuntimeError:
            self._editor = None
        # 退出时唤醒两条循环各自的调度事件及兼容事件，否则长睡会让 join 超时。
        for wake in (getattr(self, "_wake", None),
                     getattr(self, "_resched", None),
                     getattr(self, "_quote_resched", None),
                     getattr(self, "_minute_resched", None), editor_wake):
            if wake is None:
                continue
            try:
                wake.set()
            except Exception:
                pass
        # join 行情/分时（含 recover 重建后重新登记到同一属性的线程）：
        # 超时 1.5s，不阻塞退出；daemon=True 兜底。只 join 还活着且非当前线程。
        cur = threading.current_thread()
        for name in ("_quote_thread", "_minute_thread"):
            t = getattr(self, name, None)
            if t is None or t is cur or not t.is_alive():
                continue
            try:
                t.join(timeout=1.5)
            except Exception:
                pass
        # 编辑器搜索线程：QThread 无停止标志可用（run() 是一次阻塞网络调用、
        # 不检查中断），只能有限等待后放行。
        # 上限刻意压到 500ms：v1.2.x 曾因在主线程 st.wait(4000) 导致退出时明显
        # 卡 UI，后改为「收集到 pending → 后台线程等 2s → 再退出进程」。这里只做
        # 一次有界短等（给快速搜索收尾的机会），等待超时后绝不硬扛，仍由
        # quit() 的 pending 机制在后台兜底；Cmd+Q 路径无 pending 兜底，故这里的
        # 短等是它唯一的保险，但绝不能为它牺牲退出手感。
        ed = getattr(self, "_editor", None)
        if ed is not None:
            try:
                st = getattr(ed, "_search_thread", None)
            except RuntimeError:
                st = None          # 底层 C++ 对象已析构
            if st is not None:
                try:
                    if st.isRunning():
                        st.wait(500)
                except Exception:
                    pass
        # 释放底层 Socket：与线程停止同源，一并收口（quit() 不再各写一份）
        try:
            _NET_POOL.close_all()
        except Exception:
            pass

    def quit(self):
        # 幂等退出：自动关闭/用户点关闭等路径可能并发触发
        if getattr(self, "_quitting", False):
            return
        lifecycle = getattr(self, "_lifecycle_controller", None)
        if lifecycle is not None:
            if not lifecycle.begin_quit():
                return
        elif getattr(self, "_quitting", False):
            return
        else:
            self._quitting = True
        # v1.2.95：置 running / _wake.set() / join 后台线程 / 关连接池统一由
        # _shutdown_threads 完成（aboutToQuit 也挂了它），此处不再重复一份。
        self._shutdown_threads()
        # 收集仍在运行的搜索线程：不在主线程阻塞等待（原 st.wait(4000) 会卡 UI），
        # 改由后台收尾线程等待结束后再退出进程，避免 QThread 于进程退出时仍在运行导致 abort
        pending = []
        if self._editor is not None:
            st = None
            try:
                st = getattr(self._editor, "_search_thread", None)
            except RuntimeError:
                st = None
            if st is not None and st.isRunning():
                pending.append(st)
        # v1.4.18 走查：编辑窗被提前关闭（Esc/✕）后，仍在运行的 SearchThread 已
        # 被登记进 sf_editor._zombie_threads，此时 self._editor 已是 None，上面的
        # 分支收不到它。若退出只等 _editor 的线程，QApplication.quit() 会在一个仍在
        # 跑的 QThread 之上finalize → "QThread: Destroyed while thread is still
        # running" 崩溃。一并把 zombie 里运行中的线程纳入等待。
        try:
            import sf_editor
            for zt in list(getattr(sf_editor, "_zombie_threads", ())):
                try:
                    if zt is not None and zt.isRunning() and zt not in pending:
                        pending.append(zt)
                except Exception:
                    pass
        except Exception:
            pass
        # v1.2.65：停表 + 写盘 + 回收 App Nap 令牌下沉 _persist_on_exit（唯一实现）。
        # 原此处逐个停定时器（v1.2.39 还补过一次漏停的两个）后再写盘，与
        # _persist_on_exit 是同一件事的两份写法；合并后 closeEvent / aboutToQuit /
        # quit() 三条退出路径共用同一份收尾逻辑，不会再漏停定时器。
        # 位置必须保持在 join 之后 —— 后台请求停止后写盘才是干净状态。
        self._persist_on_exit()
        self._close_editor_safely()
        # R34-17：退出时一并关闭「目标价」面板，避免残留顶层窗口
        self._close_target_panel()
        # v1.2.95：原先此处还有一段关闭毛玻璃窗的代码，属 100% 不可达死代码 ——
        # _persist_on_exit 已把 _glass_window 置 None，getattr 必取到 None，整块
        # 永不执行。玻璃清理已唯一化到 _persist_on_exit。
        self.close()
        # v1.4.20：收尾全部做完、退出前最后一站，把任何还在排队的 deleteLater
        # 就地兑现（右键菜单 QMenu 等）。macOS 上下面这两个 QApplication.quit()
        # 都会直接走 -[NSApplication terminate:] → exit()，一旦有对象活到那个
        # 时刻才被销毁，就是 QWindow 析构踩空指针 —— 见 _flush_deferred_deletions。
        # 放在分支之前：_quit_after_threads 在工作线程里，那条路没法自己冲刷。
        _flush_deferred_deletions()
        if pending:
            threading.Thread(target=self._quit_after_threads,
                             args=(pending,), daemon=True).start()
        else:
            QApplication.quit()

    def _quit_after_threads(self, threads):
        """后台线程等待退出中的搜索线程结束（上限 2s），随后回主线程退出进程。
        v1.2.12：等待超时后直接放行，绝不因网络卡住的线程阻塞退出。"""
        for t in threads:
            try:
                t.wait(2000)
            except Exception:
                pass
        try:
            # QApplication.quit() 是线程安全的静态调用，会向 GUI 事件循环投递退出事件。
            # 不能在这个工作线程调用 QTimer.singleShot：该线程没有 Qt 事件循环，
            # 定时器回调可能永远不执行，造成窗口已关但进程不退。
            QApplication.quit()
        except Exception:
            pass


def _warm_universe():
    """v1.3.66：后台准备本地股票字典。任何异常都不允许影响主流程。"""
    try:
        import sf_universe
        sf_universe.ensure_ready()
    except Exception:
        pass


def _begin_runtime_session():
    """把这次运行登记进会话账本，返回 (账本, 本次会话号, 上一次会话的判定)。

    账本读不到/写不进都只留一条 SWALLOWED 痕 —— 登记失败绝不能挡在窗口起来之前。
    三项都可能是 None，调用方按 isinstance 决定后续接线，不假设账本一定在。
    """
    session = None
    session_id = None
    previous = None
    try:
        session = RuntimeSessionLedger(session_path_for_log(LOG_PATH))
        previous = session.begin(app_version=APP_VERSION, bundle_id=BUNDLE_ID)
        current = session.current()
        session_id = current.get("session_id") if isinstance(current, dict) else None
    except Exception as exc:
        log_swallow("stock_float_window.runtime_session.begin", exc)
    return session, session_id, previous


def _mark_runtime_session_clean(session, session_id):
    """正常退出那一刻把账本落成 clean_exit；失败只留痕，不打扰用户。"""
    try:
        session.mark_clean_exit(session_id)
    except Exception as exc:
        log_swallow("stock_float_window.runtime_session.clean_exit", exc)


def _offer_unfinished_session_feedback(win, session, session_id):
    """上次没正常退出时问一次要不要描述；答案必须写回账本，同一会话不重复问。

    系统框不可用（返回 None）只写一行日志 —— 问不出话与答案丢了是两件事，
    都不能把这次会话标成"已经问过"以外的状态。
    """
    import sf_system_alert
    try:
        def _answer_unfinished_session_prompt(button) -> None:
            try:
                if button == "描述问题":
                    win._queue_feedback_open()
                    session.mark_prompt(session_id, "opened_feedback")
                else:
                    session.mark_prompt(session_id, "dismissed")
            except Exception as exc:
                log_swallow("stock_float_window.runtime_session.prompt_answer", exc)

        dialog = sf_system_alert.show_alert(
            title="反馈问题",
            text="刚才似乎没有正常退出，要描述一下吗？",
            buttons=("暂不", "描述问题"), default="描述问题", cancel="暂不",
            icon_path=sf_system_alert.bundled_icon_path(), on_result=_answer_unfinished_session_prompt,
        )
        if dialog is None:
            log_line("unfinished-session prompt unavailable", "WARNING")
            return
        session.mark_prompt(session_id, "deferred")
    except Exception as exc:
        log_swallow("stock_float_window.runtime_session.prompt", exc)


if __name__ == "__main__":  # pragma: no cover - 进程入口：判断都在上面的具名函数里逐条有判据，产物冒烟关卡真跑这一段
    if len(sys.argv) > 1 and sys.argv[1] == "--verify-camera":
        from sf_camera_check import main as camera_check_main
        raise SystemExit(camera_check_main(sys.argv[2:]))
    # 冷启动优化：图标生成 + AppKit 预热放后台线程，与 QApplication 构造并行
    start_icon_worker()
    _install_global_excepthook()
    # v1.4.33：启动身份行（版本/run id/Qt 平台/生效的功能开关）。放在 excepthook
    # 之后、QApplication 之前，让最早的失败也带着身份；产物冒烟关卡以"日志里出现
    # STARTUP + first-frame"两行为启动成功的判据。
    log_startup_banner()
    app = QApplication([])
    # 目标价/替换股票是独立工具窗；它们短暂关闭时，主行情悬浮窗必须继续留在屏幕上。
    # 显式关闭“最后一个窗口自动退出”，避免 macOS 将工具窗关闭错误地连带解释为退出应用。
    app.setQuitOnLastWindowClosed(False)
    _app_ready.set()
    # v1.2.24：注册内置数字字体（随包 fonts/ 资源，任意 Mac 数字效果一致）
    _loaded = load_bundled_fonts()
    if _loaded:
        log_line("bundled fonts loaded: %r" % (_loaded,))
    # v1.3.45：把「首次字体度量」的 ~71ms 一次性开销结在窗口露出**之前**，
    # 首帧不再出现"先画一版、再重排一版"。
    warm_font_metrics()
    app.setApplicationName(APP_DISPLAY_NAME)
    if not _become_single_instance():
        # 多开保护：已有实例在运行，直接退出
        sys.exit(0)
    session, session_id, previous_session = _begin_runtime_session()
    win = FloatWindow()
    vars(win)["_runtime_session"] = session
    vars(win)["_runtime_session_id"] = session_id
    if isinstance(session, RuntimeSessionLedger) and isinstance(session_id, str):
        def _clean_exit() -> None:
            _mark_runtime_session_clean(session, session_id)
        app.aboutToQuit.connect(_clean_exit)
    _win_ref = win            # 19.2 崩溃日志上下文：暴露当前股票代码
    win.show()
    if previous_session is not None and previous_session.should_prompt and isinstance(session, RuntimeSessionLedger):
        def _offer() -> None:
            _offer_unfinished_session_feedback(win, session, previous_session.session_id)
        QTimer.singleShot(2200, _offer)
    # v1.3.45：崩溃报告检查是纯诊断，不需要挡在首帧前面 —— 挪到界面露出之后
    # 再跑（下一个事件循环空闲），把这段读盘时间从关键路径上摘掉。
    QTimer.singleShot(0, _check_recent_crashes)
    # v1.3.66：本地股票字典后台预热。延迟 3s 是为了避开启动瞬间的行情请求高峰，
    # 且字典缺失/过期才真的发请求；失败一律静默，搜索退回联网、功能不受影响。
    QTimer.singleShot(3000, _warm_universe)
    app.exec()
