"""Prepare diagnostic snapshots and deferred attachments on the UI thread.

Only snapshot capture reads the window. The returned archive contains values and
file attachment work; background finalization never reads QWidget state.
"""

from dataclasses import dataclass
from typing import Any
import math
import os
import time

from sf_constants import APP_VERSION, BETA_SUFFIX, RELEASE_STAGE
from targets import STATE_HIT
from sf_face_guard_controller import FaceGuardController
from sf_target_controller import TargetController
from sf_quote_presentation import QuotePresentationController
from sf_update_controller import UpdateController
from sf_update_flow import update_flow_diagnostics
from sf_window_lifecycle import WindowLifecycleController


@dataclass(frozen=True)
class DiagnosticSources:
    """Explicit paths and read-only providers supplied at capture time."""
    config_path: Any
    config_backup_path: Any
    last_quote_path: Any
    log_path: Any
    net_monitor: Any
    get_last_http_status: Any
    get_last_quote_source: Any
    degraded_source_streak: Any
    load_alerts: Any
    beijing_now: Any
    expired: Any
    store_prune_report: Any
    get_last_minute_source: Any
    running_bundle_id: Any
    bundle_version: Any
    peek_install_result: Any
    menu_icon_evidence: Any
    feature_snapshot: Any
    font_diagnostics: Any
    paint_diagnostics: Any
    notify_delivery_stats: Any
    calendar_sync_status: Any
    last_failures_path: Any
    update_inventory: Any
    is_crash_report_name: Any
    select_crash_reports: Any
    extract_crash_key_sections: Any
    snap_margin: int
    json: Any
    log_max: int
    log_keep: int
    bundle_id: str


def prepare_diagnostics(window, temp_out, sources: DiagnosticSources):
    from sf_diagnostics import DiagnosticArchive, write_attachments as write_diagnostic_attachments
    import sf_diag
    from sf_feedback_ui import feedback_diagnostics
    started = time.monotonic()
    diag_issues: list[str] = []
    needles = sf_diag.identity_needles(diag_issues)
    with DiagnosticArchive(temp_out) as z:
        # 运行时快照：只记录状态和计数，不记录接口地址、令牌或行情明细。
        def _public_text(text):
            return sf_diag.scrub_text(str(text), needles)

        def _public_error(exc):
            return _public_text(exc)

        def _clean_json(value, path):
            """把 Python 允许但标准 JSON 不允许的非有限数变成 null。"""
            if isinstance(value, float) and not math.isfinite(value):
                diag_issues.append("%s: non-finite number written as null" % path)
                return None
            if isinstance(value, dict):
                result = {}
                for key, item in value.items():
                    name = _public_text(key)
                    base = name
                    slot = 2
                    while name in result:
                        name = "%s#%d" % (base, slot)
                        slot += 1
                    if sf_diag.is_secret_name(str(key)) and str(key) not in (
                            "_secret_keys_dropped", "skipped_secret_named"):
                        result[name] = "<REDACTED>"
                    else:
                        result[name] = _clean_json(item, "%s.%s" % (path, name))
                return result
            if isinstance(value, (list, tuple)):
                return [_clean_json(item, "%s[%d]" % (path, index))
                        for index, item in enumerate(value)]
            if isinstance(value, str):
                return _public_text(value)
            return value

        def _xy(pos):
            """QPoint / 坐标差 → [x, y]；取不到就 None（包里只收可序列化形状）。"""
            if pos is None:
                return None
            try:
                return [int(pos.x()), int(pos.y())]
            except Exception:
                return None

        def _chart_press_now():
            """分时热区那一次按下：全局起点 + 相对窗口左上角的偏移，一次取全。

            写成一个 getter 而不是两条 lambda：偏移只在"起点存在"时才算得出，
            拆成两条就等于把同一个判空写两遍（两遍迟早会漂成两套口径）。
            """
            origin = getattr(window, "_chart_press_global", None)
            if origin is None:
                return {"origin": None, "offset": None}
            return {"origin": _xy(origin),
                    "offset": _xy(origin - window.frameGeometry().topLeft())}

        def _json_entry(name, value):
            try:
                value = _clean_json(value, name)
                data = sources.json.dumps(value, ensure_ascii=False, indent=2,
                                  sort_keys=True, allow_nan=False).encode("utf-8")
                if len(data) > 256 * 1024:
                    raise ValueError("snapshot exceeds 256KB")
            except Exception as exc:
                diag_issues.append("%s: %s" % (name, _public_error(exc)))
                return
            z.writestr(name, data)

        def _snapshot(name, fn, **kw):
            """取一份现场再写包：取不到值**必须**进 export_issues，不许静默缺键。

            为什么单独一个入口（本轮新增的十几个现场全靠它）：这些值来自
            十几个模块的 getter，装机版里任何一处抛错都可能发生；如果让它
            悄悄缺一个文件，"这一项没跑成"和"这一项一切正常"在包里就同形了
            —— 正是台账 G11/G24 那一族"闸门自己说假话"的形状。
            """
            try:
                value = fn(**kw) if kw else fn()
            except Exception as exc:
                diag_issues.append("%s: %s" % (name, _public_error(exc)))
                return
            _json_entry(name, value)

        def _field(key, fn, default=None, bucket="runtime"):
            """同一个 JSON 里**逐字段**取数：失败照常记账后用 default 占位。

            缺键与取不到在包里必须是两种长相（后者留下 default 加一行
            export_issues）—— 否则"这台机器读不出分时状态"会被读成
            "分时一切正常"，正是台账 G11/G24 那一族说假话的形状。
            `bucket` 只是让留痕写清是哪一份 JSON。
            """
            try:
                value = fn()
                return default if value is None else value
            except Exception as exc:
                diag_issues.append("%s.%s: %s" % (bucket, key, _public_error(exc)))
                return default

        def _text_entry(name, text, max_bytes=256 * 1024, tail=False):
            """写文本附件并给异常输入设上限，避免单个附件撑爆诊断包。"""
            try:
                data = str(text).encode("utf-8", "replace")
                if len(data) > max_bytes:
                    data = data[-max_bytes:] if tail else data[:max_bytes]
                    if tail:
                        data = data.partition(b"\n")[2]
                    # UTF-8 截断点可能落在多字节字符中。
                    data = data.decode("utf-8", "ignore").encode("utf-8")
                    diag_issues.append("%s: text truncated to %d bytes" %
                                       (name, max_bytes))
            except Exception as exc:
                diag_issues.append("%s: %s" % (name, _public_error(exc)))
                return
            z.writestr(name, data)

        def _read_text(src, name, max_bytes=256 * 1024, tail=False):
            with open(src, "rb") as stream:
                stream.seek(0, os.SEEK_END)
                size = stream.tell()
                offset = max(0, size - max_bytes) if tail else 0
                stream.seek(offset)
                raw = stream.read(max_bytes)
            if size > max_bytes:
                diag_issues.append("%s: source truncated, %d bytes, kept %s %d" %
                                   (name, size, "tail" if tail else "head", len(raw)))
                if offset:
                    # Do not expose a credential suffix from a cut log line.
                    raw = raw.partition(b"\n")[2]
            return raw.decode("utf-8", "replace")

        network = {
            "monitor": _field("monitor", sources.net_monitor.snapshot, {}, "network"),
            "last_http_status": _field("last_http_status",
                                         lambda: sources.get_last_http_status(), "", "network"),
            "quote_source": _field("quote_source",
                                    lambda: sources.get_last_quote_source(), "", "network"),
            # v1.4.59（S3）：连续多少次靠备源拿到行情。非零就说明主源坏了
            # 而界面看不出差别 —— 涨跌停徽标与越界拦截此刻是失效的。
            "degraded_streak": _field("degraded_streak",
                                        sources.degraded_source_streak, 0, "network"),
            # v1.4.59（S4）：存储里替别的股票留着多少条目标价（切回来还在）
            "dormant_targets": _field(
                "dormant_targets",
                lambda: max(0, len(sources.load_alerts()) - len(sources.load_alerts(window.code))),
                0, "network"),
        }
        _json_entry("network.json", network)
        def _startup_health_snapshot():
            import sf_startup_health
            return sf_startup_health.diagnostics(window)
        _snapshot("startup_health.json", _startup_health_snapshot)
        _snapshot("feedback.json", lambda: feedback_diagnostics(window))
        alerts = _field("alerts", window._safe_alerts, [], "targets")
        now = sources.beijing_now()
        target_counts = {"pending": 0, "hit": 0, "expired": 0, "invalid": 0}
        for alert in alerts:
            if not isinstance(alert, dict):
                target_counts["invalid"] += 1
            elif sources.expired(alert, now):
                target_counts["expired"] += 1
            elif alert.get("state") == STATE_HIT:
                target_counts["hit"] += 1
            else:
                target_counts["pending"] += 1
        targets_payload: dict = {
            "enabled": _field("enabled", window._targets_enabled, False, "targets"),
            "total": len(alerts),
        }
        targets_payload.update(target_counts)
        targets_payload.update({
            # v1.4.60（复核 F7）：存储超上限被剪掉休眠条目的现场。界面不加弹窗
            # （v1.4.52 口径），但"我某天设的目标价怎么不见了"必须问得出来：
            # stored_total = 存储里总条数（含别的股票留着的），
            # store_prune = 最近一次剪枝的时间/条数/被剪掉的 code，没剪过就是 {}。
            "stored_total": _field("stored_total", lambda: len(sources.load_alerts()),
                                    0, "targets"),
            "store_prune": _field("store_prune", sources.store_prune_report,
                                   {}, "targets"),
            # 诊断补全：这些条目各归属于哪只股票（"" = 无主，按顶层股票算）。
            # v1.4.59/60 那串"切股之后目标价整片消失"的根因就是归属串味，
            # 而包里此前只有总条数 —— 分不清"被我设没了"还是"被盖章走了"。
            "codes": _field("codes", window._alert_code_histogram, {}, "targets"),
        })
        _json_entry("targets.json", targets_payload)
        runtime: dict = {
            "power": _field("power", lambda: window._runtime.diagnostics(), {}),
            "maintenance": _field("maintenance", lambda: window._update_maintenance.diagnostics(), {}),
            "net_ok": bool(getattr(window, "_net_ok", False)),
            "quote_failures": _field("quote_failures",
                                       lambda: int(getattr(window, "_quote_fail", 0) or 0)),
            "minute_failures": _field("minute_failures",
                                        lambda: int(getattr(window, "_minute_fail", 0) or 0)),
            "quote_thread_alive": _field("quote_thread_alive", lambda: bool(
                getattr(window, "_quote_thread", None) and window._quote_thread.is_alive())),
            "minute_thread_alive": _field("minute_thread_alive", lambda: bool(
                getattr(window, "_minute_thread", None) and window._minute_thread.is_alive())),
            "minimized": bool(getattr(window, "_minimized", False)),
            "trading_time": _field("trading_time", lambda: bool(window._is_trading_time())),
            "update_checking": bool(getattr(window, "_update_checking", False)),
            "update_failures": _field("update_failures",
                                        lambda: int(getattr(window, "_update_failure_count", 0) or 0)),
            "update_cached_version": getattr(getattr(window, "_update_pending", None), "version", ""),
            "window": _field("window", lambda: {
                "x": window.x(), "y": window.y(), "width": window.width(), "height": window.height()}),
            # v1.4.40：人脸守卫的现场（含"到底在不在跑"与全部阈值）。
            # 单列一份是因为这类"看不见的保护"最需要被远程确认：
            # 用户报"没生效"时不必让他复现，诊断包里就有状态与失败原因。
            # v1.4.51：菜单里的灰字提示撤掉之后，`status_text` 是这句话
            # 唯一的外发通路，所以走 field_evidence（内含同一份诊断，不重复算）。
            "face_guard": _field("face_guard", window.face_guard_field_evidence),
        }
        performance_monitor = getattr(window, "_performance_monitor", None)
        runtime["performance"] = _field(
            "performance",
            (performance_monitor.export if performance_monitor is not None
             else lambda: {"available": False, "schema": "performance.v1",
                           "reason": "no window performance monitor was started"}),
            None,
        )
        # ---- 诊断补全：分时恢复现场（台账 S52 那一族的远程取证）----
        # 此前只有 minute_failures / minute_thread_alive 两个数，答不出
        # "现在是不是停在重试中、曲线是不是昨天的、数据多旧、请求还在不在飞、
        # 走的是主源还是备源" —— 而用户报的正是"重试中挂了半小时"。
        runtime["minute"] = {
            "stale": _field("minute.stale", lambda: bool(window._minute_stale), None),
            "day": _field("minute.day", lambda: window._minute_day, None),
            "in_flight": _field("minute.in_flight",
                                 lambda: bool(window._minute_busy.locked()), None),
            "points": _field("minute.points", lambda: len(window.minute_points), None),
            "last_ok_age_sec": _field(
                "minute.last_ok_age",
                lambda: (round(max(0.0, time.time() - float(window._minute_last_ok_at)), 1)
                         if float(getattr(window, "_minute_last_ok_at", 0.0) or 0.0) > 0.0
                         else None), None),
            "interval_sec": _field("minute.interval", lambda: float(window._minute_interval), None),
            "retry_left": _field("minute.retry_left", lambda: int(window._minute_retry_left), None),
            "retry_hot": _field("minute.retry_hot", lambda: bool(window._minute_retry_hot), None),
            "source": _field("minute.source", lambda: sources.get_last_minute_source(), None),
        }
        # ---- 诊断补全：更新链路的末态与身份（改名/OTA 分叉那一族）----
        # diagnostics.txt 此前只有 APP_VERSION：Beta 还是 stable、读的是哪份
        # 清单、这台机器的 bundle id 是哪一代，包里一律看不出来。
        import sf_constants as _C
        runtime["identity"] = {
            "app_version": _C.APP_VERSION,
            "bundle_id": _C.BUNDLE_ID,
            "display_name": _C.APP_DISPLAY_NAME,
            "release_stage": _C.RELEASE_STAGE,
            "manifest_url": _field("identity.manifest",
                                   lambda: _C.UPDATE_MANIFEST_URL, None),
            "running_bundle_id": _field("identity.running",
                                         lambda: sources.running_bundle_id(), None),
        }
        runtime["update"] = update_flow_diagnostics(window, _field, sources.peek_install_result)
        # 控制器自身只外发生命周期计数，不触发任何业务动作；显式引用类名让
        # 诊断覆盖闸门把这两个新模块记入 curated 闭包，而不是误判为未接线。
        runtime["controllers"] = {
            "update": _field(
                "controllers.update",
                lambda: (bool(UpdateController),
                          window._update_controller.diagnostics())[1], None),
            "lifecycle": _field(
                "controllers.lifecycle",
                lambda: (bool(WindowLifecycleController),
                          window._lifecycle_controller.diagnostics())[1], None),
            "presentation": _field(
                "controllers.presentation",
                lambda: (bool(QuotePresentationController),
                          window._quote_presentation.diagnostics())[1], None),
            "targets": _field(
                "controllers.targets",
                lambda: (bool(TargetController),
                          window._target_controller.diagnostics())[1], None),
            "face_guard": _field(
                "controllers.face_guard",
                lambda: (bool(FaceGuardController),
                          window._face_guard_controller.diagnostics())[1], None),
        }
        # ---- 诊断补全：功能开关此刻的实际生效值（1.4.37 那一族）----
        # system_drag 之类过去只在 STARTUP 行里，被轮转挤掉就等于没说。
        runtime["features"] = _field("features", lambda: sources.feature_snapshot(), None)
        # ---- 诊断补全：渲染降级末态（S35 / S39 / S49）----
        runtime["render"] = {
            "fonts": _field("render.fonts", lambda: sources.font_diagnostics(), None),
            "paint": _field("render.paint", lambda: sources.paint_diagnostics(), None),
            # 只读取图标缓存，不在导出诊断时创建图标或触发业务。
            "menu_icons": _field("render.menu_icons", sources.menu_icon_evidence, None),
        }
        # ---- 诊断补全：手势粘滞与工具窗/屏幕几何 ----
        # 这一族的投诉都是"位置错"而不是异常：面板留在原地、弹窗落在看不见
        # 的副屏、吸附够不着就静默不动 —— log_swallow 天生抓不到它们。
        runtime["gesture"] = {
            "drag": _field("gesture.drag", lambda: bool(window._drag), None),
            "sys_drag": _field("gesture.sys_drag", lambda: bool(window._sys_drag), None),
            "resizing": _field("gesture.resizing", lambda: bool(window._resizing), None),
            # 近两天核对补的一条（v1.4.71 修"分时区拖不动"留下的现场）：
            # 那次投诉在朋友机器上，包里没有"按下到底落在哪个分支"这一问，
            # 于是"是热区判定错了、还是拖窗分支没进去"只能靠复现来分。
            # press_chart = 按下命中了分时热区（还没过移动阈值前的待判定态）；
            # chart_press = 那一刻的全局起点；offset = 起点减窗口左上角，
            # 非零却 drag 为假 = "按下了、拖了、窗口没动"这一类的直接证据。
            "press_chart": _field("gesture.press_chart", lambda: bool(window._press_chart), None),
            "chart_press": _field("gesture.chart_press", _chart_press_now, None),
        }
        runtime["tools"] = _field("tools", lambda: window._tool_window_geometries(), None)
        # 拉伸/收展动画的在飞现场（1.4.34 那一族：把手能拖到 0 高、动画与手势
        # 抢同一根高度轴）。"窗口被压没了"往往在用户开口时已经自愈，只剩结果
        # 没有原因 —— 而 minimumHeight 被改到多少、动画在往哪儿走，只有此刻知道。
        runtime["stretch"] = {
            "anim_from": _field("stretch.from",
                                lambda: int(getattr(window._anim_driver,
                                                    "layout_anim_from", 0) or 0), None),
            "anim_to": _field("stretch.to",
                              lambda: int(getattr(window._anim_driver,
                                                  "layout_anim_to", 0) or 0), None),
            "min_height": _field("stretch.min_height",
                                 lambda: int(window.minimumHeight()), None),
            "height_now": _field("stretch.height_now", lambda: int(window.height()), None),
        }
        runtime["screens"] = _field("screens", lambda: window._screen_geometries(), None)
        runtime["snap_margin"] = _field("snap_margin", lambda: int(sources.snap_margin), None)
        _json_entry("runtime.json", runtime)
        # v1.4.46：玻璃层现场 + 最近 30 分钟的失效锚点，单独一份 glass.json。
        # 为什么单独列：「只剩背景 / 内容看不见」这一类 8 条线上逃逸的共同点是
        # **现场只在用户那台机器上** —— 本机日志能给到"一次 1278 秒 / 482 行倒序
        # 检出"的强度，但用户机器上的 debug.log 过去没有任何回传通路，D-04/D-31
        # 因此至今写着"未确认，需现场取证"，这一类的定位时长也永远记不到。
        # 诊断包是已有且唯一的外发通路，所以走它，而不是新加菜单项或偷偷上传。
        # 时刻口径由 sf_glass.glass_diagnostics 自己写在返回值里，不靠这里补注释。
        # 本轮补两件玻璃层自己答不出的：**拖拽期间掉过多少像素**（1.4.31/37
        # 那条"背景慢半拍"的量化证据，只在跟随会话内累计），以及
        # `_force_top_forced` 有没有泄漏成真（真值时每一拍都全量置顶，
        # 表现只有卡顿、没有任何痕迹）。
        _glass_diag = _field("snapshot", lambda: dict(window.glass_diagnostics()), {}, "glass")
        _glass_diag["follow_lag_px"] = _field("follow_lag_px", lambda: round(
            float(getattr(window, "_glass_follow_lag_px", 0.0) or 0.0), 1), None, "glass")
        _glass_diag["force_top_forced"] = bool(getattr(window, "_force_top_forced", False))
        # 近两天核对补的一条（v1.4.70 修"浏览器全屏时右键弹不出菜单"）：
        # 那条链的判据是"补到 isOnActiveSpace 为真为止，最多 50 拍"，可当时
        # 到底补了几拍、有没有弹层卡在名单里、表停没停 —— 包里一处都读不出来，
        # 于是"这台机器上到底补齐了没有"只能让用户再复现一次。
        # 取的是 sf_glass 公开的那两问（popup_space_pending / …_timer_running），
        # 不在这里重算形状：名单与上限只有一份实现。
        def _popup_space():
            import sf_glass
            pending = sf_glass.popup_space_pending()
            return {
                "pending": len(pending),
                "tries": [[type(w).__name__, int(n)] for w, n in pending][:8],
                "timer_running": bool(sf_glass.popup_space_timer_running()),
                "tick_ms": sf_glass.POPUP_SPACE_TICK_MS,
                "max_ticks": sf_glass.POPUP_SPACE_MAX_TICKS,
            }
        _glass_diag["popup_space"] = _field("glass.popup_space", _popup_space, None, "glass")
        _json_entry("glass.json", _glass_diag)
        # ---- 诊断补全：三份"此前只能靠用户复现"的现场，各成一份文件 ----
        # notify.json：到价提醒到底发出去没、走的哪条通道（台账 S34 ——
        #   整条 UN 路线静默坏死几十个版本，现场只有一行会被挤掉的 WARNING）。
        # universe.json：本地证券字典是否可用/过期/报过什么错，加编辑器的
        #   拒绝计数（1.4.51 的"510300 加不进去"至今分不清三种原因）。
        # calendar.json：休市日历各月的同步状态 —— runtime.trading_time 只给
        #   结论，日历拉不到时"该不该刷新"判错了没有依据可查。
        _snapshot("notify.json", sources.notify_delivery_stats)
        def _universe_snapshot():
            import sf_universe
            data = dict(sf_universe.stats())
            # path 含家目录（里面有用户名）：诊断包要发给别人，只把前缀换成 ~，
            # 目录结构照留 —— "落在哪个代的数据目录"本身就是排障信息。
            _home = os.path.expanduser("~")
            _p = str(data.get("path") or "")
            data["path"] = ("~" + _p[len(_home):]) if _home and _p.startswith(_home) else _p
            data["index_ready"] = sf_universe.index_ready()
            return data
        _snapshot("universe.json", _universe_snapshot)
        _snapshot("calendar.json", sources.calendar_sync_status)
        # state.json：自动采集层（sf_diag）。上面那些文件都是**人工立意**的现场
        # —— 每项回答"这项坏了用户会说什么"。这一份不同：它按命名约定把产品
        # 模块与实例字典里的状态位扫进包，所以新加一个 `self._xxx_fail_count`
        # 不必回来接线。立意那一半由发版链第 3.9 关
        # （tools/diag_coverage_check.py）硬拦，见 AGENTS §5.10。
        def _auto_state():
            import sf_diag
            return sf_diag.collect(window)
        _snapshot("state.json", _auto_state)
        paths = {"config": sources.config_path, "backup": sources.config_backup_path,
                 "last_quote": sources.last_quote_path, "log": sources.log_path,
                 "failures": sources.last_failures_path(),
                 "crashes": os.path.expanduser("~/Library/Logs/DiagnosticReports")}
        metadata = {"version": APP_VERSION, "code": window.code,
                    "last_ok_time": window._last_ok_time or "",
                    "quote_source": network["quote_source"],
                    "http_status": network["last_http_status"],
                    "stage": RELEASE_STAGE, "bundle_id": sources.bundle_id,
                    "version_label": _field("version_label", lambda: "v" + sources.bundle_version()
                        + (BETA_SUFFIX if RELEASE_STAGE == "beta" else ""), "", "diagnostics"),
                    "running_bundle_id": _field("running_bundle_id", sources.running_bundle_id, "", "diagnostics"),
                    "log_keep": sources.log_keep, "log_max": sources.log_max,
                    "started": started}
        helpers = {"json_entry": _json_entry, "text_entry": _text_entry,
                   "read_text": _read_text, "public_text": _public_text, "field": _field,
                   "inventory": sources.update_inventory, "is_crash": sources.is_crash_report_name,
                   "select_crashes": sources.select_crash_reports,
                   "extract_crash": sources.extract_crash_key_sections}
        z.defer(lambda: write_diagnostic_attachments(z, paths, metadata, diag_issues, helpers))
    return z
