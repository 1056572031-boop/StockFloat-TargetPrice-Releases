# -*- coding: utf-8 -*-
"""「目标价」功能：交易日历。

- 2026 年：直接复用内置 TRADING_HOLIDAYS（sf_constants），**不发网络请求**。
- >2026：优先内存缓存 → 磁盘缓存；缓存缺失时**降级为"仅按周末判断"**，
  并在后台线程异步拉取深交所官方月表补齐（下次调用即生效）。
- **调用线程（含 UI 线程）永不发起网络请求**（R32 修复：原实现在
  is_holiday 里同步 http_get，弱网/超时会让界面卡住最长约 16 秒）。

缓存：~/Library/Application Support/StockFloatTargetPrice/calendar_YYYY-MM.json（按月分文件）

数据源限制（重要）：深交所月表通常只发布到当年 12 月，未发布的月份
返回 0 行。空表一律视为"拉取失败"——不落盘、不标记为已拉取、允许重试，
否则会把"无数据"误当成"该月无休市日"永久缓存（R32-1 毒缓存）。
"""
import json
import os
import threading
import time
from datetime import timedelta

from sf_constants import APP_SUPPORT_DIR, TRADING_HOLIDAYS
from sf_config import _atomic_write_json
from sf_logging import log_line, log_line_throttled
from sf_network import http_get

_CACHE_DIR = APP_SUPPORT_DIR
_LOCK = threading.RLock()
_month_cache = {}       # "YYYY-MM" -> set of "YYYY-MM-DD" 休市日
_fetched_months = set()  # 已从网络成功拉取的月份（不再重复请求）
_inflight = set()       # 正在后台拉取的月份
_next_retry_at = {}     # "YYYY-MM" -> epoch seconds（失败后最小重试间隔）
_month_sync_status = {} # "YYYY-MM" -> ready / syncing / degraded（仅供 UI 非阻断提示读取）

SZSE_URL = "https://www.szse.cn/api/report/exchange/onepersistenthour/monthList?month="

_RETRY_INTERVAL = 60.0   # 拉取失败后至少隔 60s 才重试，避免请求风暴
_MAX_SCAN_DAYS = 400     # next_trading_day 扫描上限，防止数据异常时死循环


def _month_cache_path(ym):
    return os.path.join(_CACHE_DIR, "calendar_%s.json" % ym)


def _load_month_cache(ym):
    if ym in _month_cache:
        return _month_cache[ym]
    hol = set()
    try:
        with open(_month_cache_path(ym), "r", encoding="utf-8") as f:
            data = json.load(f)
        values = data.get("holidays") if isinstance(data, dict) else None
        if not isinstance(values, list) or not values:
            raise ValueError("calendar cache has no holidays")
        hol = set(values)
        # 磁盘缓存来自此前已成功拉取，重启后同样是已确认日历；若不恢复
        # ready 状态，目标价会整月停留在“等待同步”而无法触发或按时清理。
        _fetched_months.add(ym)
        _month_sync_status[ym] = "ready"
    except Exception:
        hol = set()
    _month_cache[ym] = hol
    return hol


def _save_month_cache(ym, holidays):
    _month_cache[ym] = set(holidays)
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        path = _month_cache_path(ym)
        # v1.3.38：与配置同源的原子写 + 落盘。半截的月表会被 _load_month_cache
        # 判为"无 holidays"而静默丢弃，等于白拉一次；断电留下坏文件更会让
        # 目标价整月不准。
        _atomic_write_json(path, {"holidays": list(holidays)})
        # 0600：日历决定目标价是否在法定假日被清空，被同机其他账户改写就能
        # 让提醒静默失效，故只允许本人写。
        os.chmod(path, 0o600)
    except Exception:
        pass


def _fetch_month_holidays(ym):
    """拉取深交所月表，返回该月休市日 set（字符串）；**空表/失败返回 None**。

    仅用于 >2026-12 的月份；2026 由内置集合覆盖，不走本函数。

    官方未发布的月份会返回 0 行 —— 必须视为失败返回 None，
    否则会把"无数据"当成"该月无休市日"落盘永久生效（R32-1）。

    v1.4.59（S5）：四种失败原因**各自留痕**。旧实现是 `except Exception: return None`
    一句零日志，于是"未来某月永远拉不到"这件事在界面上只剩一个 degraded 角标，
    事后完全查不到原因（网络挂了？字段改名？官方换了响应形状？）——
    而后果是法定假日被当交易日：目标价在假日被清空、提醒口径全错。
    按 §6 第 9 条，兜异常可以，静默不行。
    """
    try:
        txt = http_get(SZSE_URL + ym, gbk=False, timeout=8, retries=1)
    except Exception as exc:
        log_line("calendar %s 拉取失败（网络）：%r" % (ym, exc), "WARNING")
        return None
    if not txt:
        log_line("calendar %s 拉取失败：响应为空" % ym, "WARNING")
        return None
    try:
        data = json.loads(txt)
    except Exception as exc:
        log_line("calendar %s 响应不是合法 JSON：%r（前 80 字节 %r）"
                 % (ym, exc, txt[:80]), "WARNING")
        return None
    rows = data.get("data", []) if isinstance(data, dict) else []
    if not rows:
        # 官方未发布 = 常态，不该刷 WARNING；但必须能查到"这个月为什么一直没确认"
        log_line_throttled("calendar.empty.%s" % ym,
                           "calendar %s 官方返回 0 行（未发布），按失败处理且不落盘" % ym,
                           level="INFO", every=20)
        return None
    hol = set()
    # 深交所字段名为拼音缩写：jybz = 交易标志（"0" 表示当日休市），
    # jyrq = 交易日期(YYYY-MM-DD)。换数据源或字段改名时这里最先失效。
    for row in rows:
        if str(row.get("jybz")) == "0":
            jyrq = row.get("jyrq")
            if jyrq:
                hol.add(jyrq)
    if not hol:
        log_line("calendar %s 形态异常：有 %d 行却一个休市日都没解析出来"
                 "（字段改名 jybz/jyrq？数据源换了？）" % (ym, len(rows)), "WARNING")
        return None  # 任何一个月都至少有周末休市日，空集合说明响应异常
    return hol


def _peek_month(ym):
    """只读缓存（内存 → 磁盘）。**不发网络请求**，可在 UI 线程调用。"""
    with _LOCK:
        hol = _month_cache.get(ym)
        if hol is None:
            hol = _load_month_cache(ym)
        return hol


def calendar_sync_status(yms=None):
    """返回未来月份日历的只读同步状态，不触发网络或阻塞调用线程。"""
    with _LOCK:
        keys = list(yms) if yms is not None else list(_month_sync_status)
        return {ym: _month_sync_status.get(ym, "ready" if ym in _fetched_months else "idle")
                for ym in keys}


def _ensure_month(ym):
    """确保 ym 的休市日已就绪，必要时发起网络拉取。返回 set（可能为空）。

    会阻塞（网络 I/O），**只允许在后台线程调用**。
    """
    with _LOCK:
        hol = _month_cache.get(ym)
        if hol is None:
            hol = _load_month_cache(ym)
        if ym in _fetched_months:
            _month_sync_status[ym] = "ready"
            return hol
        if _next_retry_at.get(ym, 0) > time.time():
            _month_sync_status[ym] = "degraded"
            return hol

    fetched = _fetch_month_holidays(ym)
    if not fetched:
        # 空集合同样视为失败：任何一个月都至少有周末休市日，
        # 空结果意味着"官方未发布/响应异常"，落盘会造成毒缓存（R32-1）。
        with _LOCK:
            _next_retry_at[ym] = time.time() + _RETRY_INTERVAL
            _month_sync_status[ym] = "degraded"
        return hol  # 降级：沿用缓存（可能为空）
    with _LOCK:
        _save_month_cache(ym, fetched)
        _fetched_months.add(ym)
        _month_sync_status[ym] = "ready"
    return fetched


def _warm_async(ym):
    """后台线程预热某个月份；已在拉取/已成功/处于重试冷却期内则跳过。

    返回启动的 Thread（跳过时返回 None）——便于测试里 join 等待，
    避免测试依赖真实网络。
    """
    with _LOCK:
        if ym in _fetched_months or ym in _inflight:
            return None
        if _next_retry_at.get(ym, 0) > time.time():
            _month_sync_status[ym] = "degraded"
            return None
        _inflight.add(ym)  # 立即占位，避免并发重复起线程
        _month_sync_status[ym] = "syncing"

    def _run():
        try:
            _ensure_month(ym)
        except Exception:
            pass
        finally:
            with _LOCK:
                _inflight.discard(ym)

    t = threading.Thread(target=_run, name="sf-cal-%s" % ym, daemon=True)
    t.start()
    return t


def prefetch_months_async(yms):
    """后台预热若干月份（启动时调用，避免首次使用时才去拉）。

    yms: 可迭代的 "YYYY-MM"。无效项会被忽略。
    """
    for ym in yms or ():
        if not ym or not isinstance(ym, str):
            continue
        _warm_async(ym)


def is_holiday(d):
    """d: date。返回是否休市日。

    2026：查内置 TRADING_HOLIDAYS（不发请求）。
    >2026：只读缓存；缓存缺失时降级为 False（按周末判断）并后台补齐。
    <2026：历史月份不发请求，直接按周末判断。
    """
    s = d.strftime("%Y-%m-%d")
    if d.year == 2026:
        return s in TRADING_HOLIDAYS
    if d.year < 2026:
        # 历史月份不发请求：过去的日期不会再有目标价到期，按"非休市"交给
        # 周末规则兜底即可，没必要为它付一次网络往返。
        return False
    ym = d.strftime("%Y-%m")
    hol = _peek_month(ym)
    if hol:
        return s in hol
    _warm_async(ym)  # 缓存缺失：降级判断 + 后台补齐，绝不阻塞调用线程
    return False


def is_trading_day_confirmed(d):
    """返回 True / False / None：交易日已确认 / 已确认休市 / 日历尚未就绪。

    对未来年份，不能把“缓存未到”直接当成“正常交易日”。目标价若在法定假日被
    暂判为生效日，15:00:30 定时清空会让提醒永久丢失；None 让调用方延后清空，
    待官方日历准备好再做最终判断。
    """
    if d.weekday() >= 5:
        return False
    if d.year <= 2026:
        return not is_holiday(d)
    ym = d.strftime("%Y-%m")
    with _LOCK:
        ready = ym in _fetched_months
    if not ready:
        _warm_async(ym)
        return None
    return not is_holiday(d)


def is_trading_day(d):
    """d: date。返回是否交易日（非周末且非休市日）。

    兼容既有调用：未来月份未就绪时仍按工作日临时判断；涉及目标价清空的调用
    必须改用 is_trading_day_confirmed，不能将该临时判断作为最终结论。
    """
    confirmed = is_trading_day_confirmed(d)
    return confirmed if confirmed is not None else True


def next_trading_day(d):
    """d 之后第一个交易日（date）。周末 + is_holiday 跳过。

    扫描上限 _MAX_SCAN_DAYS 天：日历数据异常（恒为休市）时不会在
    调用线程里死循环，超限降级返回扫描到的第一个工作日。
    """
    nd = d + timedelta(days=1)
    fallback = None
    for _ in range(_MAX_SCAN_DAYS):
        if nd.weekday() >= 5:
            nd += timedelta(days=1)
            continue
        if fallback is None:
            fallback = nd
        if is_holiday(nd):
            nd += timedelta(days=1)
            continue
        return nd
    return fallback if fallback is not None else nd
