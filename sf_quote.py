# -*- coding: utf-8 -*-
"""模块化拆分 v1.2.13：sf_quote —— 由 stock_float_window.py 搬移生成，行为零变化。

行情字典（quote dict）字段契约 —— 腾讯主源与新浪备源共用以下行情键，
分时接口另返回 (time, price, avg) 点集。消费方（_apply_quote、quote_status、涨跌停拦截）
依赖这些字段；改键名等于改对外契约：

    键           含义 / 单位          口径
    name         标的名称             字符串原样
    code         标的代码             主源保留响应代码（通常为 6 位数字），
                                      备源保留请求代码；fetch_quote 的请求入口经 normalize_code
    price        现价（元）           出口按 price_decimals 归一小数位：股票 2 位，
                                      基金/ETF/可转债/B股/港股 3 位 —— 两源对同一标的
                                      返回位数不同是 v1.2.65 修掉的"切源字号抖动"根源
    pre_close    昨收（元）           字符串原样
    change       涨跌额（元）         主源保留有效扩展字段文本，缺失/非法/非有限时置空；
                                      备源由现价和昨收计算，昨收<=0 时置空串显示 "--"，
                                      计算异常沿用 "0.0" 兜底（v1.2.50 H-3）
    pct          涨跌幅（%）          主源同样读取扩展字段，备源按昨收计算，容错同 change
    high/low     今日最高/最低（元）   尾部字段越界/空/非法/非有限 → 空串（渲染层不显示）
    volume       成交量               仅主源有（腾讯 f36 接口原值）；工程内只做 >0 的
                                      "是否在交易"判定，不做单位换算、不做数值展示
    limit_up/    涨停/跌停价（元）     仅主源有；字段缺失时徽标可复用板块估算，
    limit_down                        目标价越界校验只认官方字段（见 official_limit_prices）

主源 = 腾讯（QUOTE_URL），备源 = 新浪（QUOTE_URL_FALLBACK）。主源"格式错误"（非网络类）
连续达到 FORMAT_ERROR_ALERT_THRESHOLD 会发醒目 ERROR；"主源坏了但备源一直有数据"这件事
由 degraded_source_streak() 记账并进诊断包 —— 那是 v1.4.59 之前几个月无人察觉的静默降级。
"""

import collections
import datetime
from datetime import date
import json
import math
import re
import sys
import threading
import time
import urllib.request, urllib.parse
from sf_constants import (CONFIG_PATH, EASTMONEY_SEARCH_URL, EASTMONEY_TOKEN_DEFAULT,
                          FG_DIM, MINUTE_HTTP_RETRIES, MINUTE_HTTP_TIMEOUT,
                          MINUTE_URL, MINUTE_URL_FALLBACK, QUOTE_URL,
                          QUOTE_URL_FALLBACK, SEARCH_BJ_FALLBACK_MIN, SEARCH_BJ_TIMEOUT,
                          SEARCH_CACHE_MAX, SEARCH_CACHE_TTL, SEARCH_MAX_RESULTS,
                          SEARCH_RETRIES, SEARCH_TIMEOUT, SEARCH_URL,
                          SINA_REFERER)
from sf_logging import log_line
from sf_network import QuoteDataError, get_last_http_status, http_get
# 交易所判定唯一实现在 sf_utils；它只依赖标准库，不回引行情或 Qt 模块。
from sf_utils import market_prefix, normalize_code
# v1.2.80：休市判定统一走 sf_calendar.is_holiday（内置 2026 + 深交所未来月表缓存，
# 调用线程上不发网络请求），避免 market_status/_is_trading_time 只认 2026 内置表、
# 在 2027+ 节假日把休市误判为交易日。
from sf_calendar import is_holiday
# v1.3.66：本地股票字典（全 A 代码/名称表）。导入即注册，搜索时先查它。
import sf_universe as universe

_LAST_QUOTE_SOURCE = None     # 最近一次成功行情来源："tencent"/"sina"


class QuoteData(dict):
    """Keep provider time outside the stable, public quote dictionary keys."""
    def __init__(self, values, timestamp=""):
        super().__init__(values)
        self.source_time: datetime.datetime | None = None
        self.received_at = _beijing_now()
        for pattern in ("%Y%m%d%H%M%S", "%Y-%m-%d %H:%M:%S"):
            try:
                self.source_time = datetime.datetime.strptime(str(timestamp), pattern).replace(
                    tzinfo=datetime.timezone(datetime.timedelta(hours=8)))
                break
            except ValueError:
                continue


def quote_is_current(quote, now=None):
    """Real responses require source time; ordinary dict callers keep compatibility."""
    if not isinstance(quote, QuoteData):
        return True
    stamp = quote.source_time
    if stamp is None:
        return False
    now = now or _beijing_now()
    if now.tzinfo is None:
        now = now.replace(tzinfo=datetime.timezone(datetime.timedelta(hours=8)))
    age = (now - stamp).total_seconds()
    if age < -60:
        return False
    if _is_trading_time(now):
        return stamp.date() == now.date() and age <= 120
    day = now.date()
    if now.hour * 60 + now.minute < 9 * 60 + 15:
        day -= datetime.timedelta(days=1)
    for _ in range(32):
        if day.weekday() < 5 and not is_holiday(day):
            return stamp.date() == day
        day -= datetime.timedelta(days=1)
    return False


def ensure_current_quote(code, quote):
    """A stale primary response can use a current backup, never a fake fresh time."""
    if quote_is_current(quote):
        return quote
    fallback = fetch_quote_fallback(code)
    if not quote_is_current(fallback):
        raise QuoteDataError("quote source time is missing or stale")
    global _LAST_QUOTE_SOURCE
    _LAST_QUOTE_SOURCE = "sina"
    _note_degraded_source(code, "primary quote time stale")
    return fallback

def get_last_quote_source():
    """第1轮走查修复：最近成功行情来源的读取 getter。
    跨模块禁止 from...import 直接引用 _LAST_QUOTE_SOURCE —— 那会复制导入时快照，
    恒为 None 导致诊断导出 quote_source 永为空（与 sf_network B1 同型问题）。
    统一经本函数实时读取最新值。"""
    return _LAST_QUOTE_SOURCE

# "非网络类格式错误"连续告警阈值：按 kind 分桶计数（quote / minute 各自独立），
# 主源一次成功即复位（_reset_format_error）。阈值含义是"接口返回了 200 但内容不像
# 行情"，这类问题重试无用、只能靠人改代码 —— 所以要够响（ERROR 级、带代码与原文片段）。
FORMAT_ERROR_ALERT_THRESHOLD = 3

# ---- v1.4.59（S3 第二处）：长期跑备源这件事必须能查到 ------------------------------
# 旧事实：主源格式永久变更而新浪备源正常时，备源成功会把格式错误计数**复位**
# （v1.2.69 为了防刷屏特意做的），于是 `QUOTE FORMAT ERROR x3` 这道告警永远不响，
# 程序可以几个月都跑在备源上而没人知道。备源的 dict 没有 volume/limit_up/limit_down
# → 涨跌停徽标与"目标价不得越过涨跌停"的拦截**同时静默失效**。
# 复位本身是对的（别把"其实有数据"报成"接口挂了"），缺的是**另一个计数器**：
# 主源坏、靠备源撑着的连续次数。它只在主源成功时归零，并在诊断包里可读。
# v1.4.62 删掉了 `DEGRADED_SOURCE_ALERT_AFTER = 20`：v1.4.60 把提示改成按时间节流之后
# 它就再没有消费者了，而注释还写着"这里按次计" —— 一个没人读、说法还过时的常量，
# 比没写更坏（下一个人会以为 20 次这个数真的决定了什么）。累计次数照旧由
# degraded_source_streak() 提供，进诊断包 runtime.json 的 degraded_streak。
# v1.4.60（复核 F5）：日志节流按**时间**。原先是次数取模，而盘中 0.1s 节拍下
# 20 次就是 2 秒 —— 主源坏一整天会写约 1800 条 WARNING，把 debug.log 的 7 天轮转
# 整个挤掉，恰好造成本轮想消灭的那类"事后查不到"（sf_presence 里为同一件事写过警示）。
# 现在：首条必记，其后每 DEGRADED_SOURCE_LOG_SEC 秒最多一条，并附累计次数。
DEGRADED_SOURCE_LOG_SEC = 60.0
_degraded_lock = threading.Lock()
_degraded_streak = 0
_degraded_last_log = 0.0


def _note_degraded_source(code, exc):
    """记一次"主源失败、备源兜住"。首条必记，其后按时间节流（附累计）。"""
    global _degraded_streak, _degraded_last_log
    with _degraded_lock:
        _degraded_streak += 1
        n = _degraded_streak
        now = time.monotonic()
        due = n == 1 or (now - _degraded_last_log) >= DEGRADED_SOURCE_LOG_SEC
        if due:
            _degraded_last_log = now
    if due:
        log_line("行情已连续 %d 次跑备源（code=%s，主源原因 %r）—— "
                 "备源无 volume/limit_up/limit_down，涨跌停徽标与"
                 "「目标价不得越过涨跌停」的拦截当前是失效状态"
                 % (n, code, exc), "WARNING")


def degraded_source_streak():
    """诊断包读取口：连续多少次靠备源拿到行情（主源成功即归零）。"""
    with _degraded_lock:
        return _degraded_streak


def _reset_degraded_source():
    """主源恢复成功时调用（仅 fetch_quote 主源分支一处）：连击与日志手表一起归零。

    连击（_degraded_streak）归零是"主源又活了"；手表（_degraded_last_log）归零是
    让**下一次**降级的第一条日志真的记下来 —— 否则下午那次降级可能被上午那条
    60 秒内的记录顶掉，等于又一次"事后查不到"。
    """
    global _degraded_streak, _degraded_last_log
    with _degraded_lock:
        _degraded_streak = 0
        # 主源活着就把手表也归零：下一次再降级时"首条必记"要真的记下来，
        # 否则下午那次降级可能被上午那条 60 秒内的记录顶掉。
        _degraded_last_log = 0.0

def load_api_urls():
    """1.3 v1.2.0：从 config.json 的 "api_urls" 字段加载 API 地址覆盖全局常量。
    仅覆盖配置中存在的 key，未配置的保持内置默认；配置缺失/解析失败时静默回落。
    这样改 API 地址只需编辑 config.json，无需重新打包。返回覆盖的 key 列表（日志用）。
    v1.2.39：新增 https 强制校验 —— 仅接受 https:// 开头的地址；http 明文或
    非法值一律不生效并记录 WARNING 日志（防明文传输/防脏数据注入），回退内置默认。"""
    changed = []
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        api = d.get("api_urls")
        if not isinstance(api, dict):
            return changed
        from sf_credentials import resolve_config_urls
        api = resolve_config_urls(CONFIG_PATH, d)
        mapping = {
            "quote": "QUOTE_URL",
            "quote_fallback": "QUOTE_URL_FALLBACK",
            "minute": "MINUTE_URL",
            "minute_fallback": "MINUTE_URL_FALLBACK",
            "search": "SEARCH_URL",
            "eastmoney_search": "EASTMONEY_SEARCH_URL",
            "sina_referer": "SINA_REFERER",
        }
        for key, gname in mapping.items():
            v = api.get(key)
            if not (isinstance(v, str) and v.strip()):
                continue
            v = v.strip()
            if not v.lower().startswith("https://"):
                log_line("api_urls: reject non-https %s (%s) -> keep default" % (key, gname), "WARNING")
                continue
            globals()[gname] = v
            changed.append(key)
    except Exception:
        # 配置缺失/损坏/无读取权限都是正常场景，静默回落内置默认地址，不阻断启动。
        pass
    if changed:
        log_line("api_urls loaded from config: %s" % ",".join(changed), "INFO")
    return changed

_format_err_count = {"quote": 0, "minute": 0}
# v1.2.71 第4轮走查：计数由行情线程与分时线程并发读写，get→+1→set 不是原子
# 操作，两条线程交错时会丢掉一次自增 —— 表现为阈值告警迟到（本该 N 次告警
# 变成 N+1 次）或彻底不触发。加锁保证计数单调。
_format_err_lock = threading.Lock()

def _bump_format_error(kind, tag, code, exc):
    """格式错误（非网络错误）计数自增；达到阈值输出醒目 ERROR 诊断日志。
    告警后重置计数避免反复刷屏；正常流程不受影响。"""
    with _format_err_lock:
        n = _format_err_count.get(kind, 0) + 1
        if n >= FORMAT_ERROR_ALERT_THRESHOLD:
            _format_err_count[kind] = 0
        else:
            _format_err_count[kind] = n
    if n >= FORMAT_ERROR_ALERT_THRESHOLD:
        log_line("%s FORMAT ERROR x%d (code=%s): %r —— 非官方接口格式疑似变更，请核查 %s 数据源"
                 % (tag, n, code, exc, kind), "ERROR")
        return True
    return False

def _reset_format_error(kind):
    """该数据类（"quote" / "minute"）的格式错误连击归零 —— 主源成功即调，别在别处调。

    只复位对应 kind，不互相牵连：行情主源恢复不该抹掉分时的故障记录，反之亦然。
    """
    with _format_err_lock:
        _format_err_count[kind] = 0

def _secid(code):
    """东财 secid 拼接：sh→1.xxx，sz/bj→0.xxx，港股 hk→116.xxx，美股 us→105.xxx。
    裸代码优先按 market_prefix 判市场（含基金/ETF/固收），未知段保留历史兜底。

    v1.2.71 第4轮走查：港股/美股此前会穿过所有分支落到最后的裸串兜底，
    原样返回 "hk00700"/"usaapl" —— 这不是东财的合法 secid，主源故障时
    备源必然取不到数据。现按东财市场段位映射。
    已知限制：美股段位按交易所区分（105=NYSE / 106=NASDAQ / 107=AMEX），
    无法仅凭代码推断，此处统一取 105；纳斯达克标的备源可能取空 —— 备源
    本就是降级路径，取空不影响主源（腾讯）的正常展示。
    """
    # v1.3.42 第4轮走查：入参必须先 strip。带空格的代码（粘贴自选、其它版本
    # 写入的配置）会让 startswith 全部落空、也不满足 isdigit，最终原样回传
    # " sh600519 " —— 东财备源拿到带空格的 secid 恒返回空，且没有任何报错。
    c = (code or "").strip().lower()
    if c.startswith("sh"):
        return "1." + c[2:]
    if c.startswith("bj"):
        # v1.3.49：与 _tencent_code 同口径 —— 东财 secid 需要 6 位数字，北交所老代码
        # （bj8307 这类 4 位码）数字部分不足 6 位时右侧补 "0"，否则东财恒返回空。
        digits = c[2:]
        if digits.isdigit() and len(digits) < 6:
            digits = digits.ljust(6, "0")
        return "0." + digits
    if c.startswith("sz"):
        return "0." + c[2:]
    if c.startswith("hk"):
        return "116." + c[2:]
    if c.startswith("us"):
        return "105." + c[2:]
    if c.isdigit():
        # v1.4.18 走查：裸数字代码的市场段位统一走 sf_utils.market_prefix（唯一实现），
        # 与 _tencent_code 同口径。此前这里是 v1.2.65 之前就被废弃的老兜底
        # `6/9→沪、其余→深`，会把 5xxxxx 沪市基金/ETF（510300、588000）、
        # 11xxxx 沪市可转债误判成深市 secid（0. 段）→ 东财备源分时恒返回空，
        # 主源（腾讯）故障时这些标的没有备源，且没有任何报错。
        digits = c
        if len(digits) == 4 and digits.startswith(("4", "8")):
            digits = digits + "00"          # 北交所 4 位老代码补零，与 normalize_code 一致
        mp = market_prefix(digits[:6])
        if mp == "sh":
            return "1." + digits
        if mp in ("sz", "bj"):
            return "0." + digits
        # market_prefix 判定不了（非 6 位/未知段）时保留旧兜底，不改变既有可用行为。
        if c.startswith("920"):
            return "0." + c
        return ("1." if c[0] in ("6", "9") else "0.") + c
    return c

def _tencent_code(code):
    """腾讯行情接口市场前缀：裸代码经 market_prefix 补 sh/sz/bj，未知段仍兜底 sz。
    腾讯接口自 2026-08 起对裸代码返回 code param error，必须带市场前缀。
    v1.2.48(S1)：bj 前缀的 4 位数字补 0 到 6 位（bj8307→bj830700），与北交所
    行情接口返回的 6 位代码对齐；已满 6 位或非纯数字保持原样。"""
    c = (code or "").lower().strip()
    if c.startswith("bj"):
        digits = c[2:]
        if digits.isdigit() and len(digits) < 6:
            digits = digits.ljust(6, "0")
        return "bj" + digits
    # v1.2.71 第4轮走查：港股/美股原样返回（腾讯接口本就用 hk00700 / usaapl 形式）。
    # 此前这两类会穿过分支落到下方裸代码兜底被强加 "sz" 前缀 —— 实测
    # hk00700→"szhk00700"、usaapl→"szusaapl"，腾讯接口恒返回空，自选美股/港股时
    # 行情与分时主源双双失效（v1.2.69 只修了新浪备源的 _sina_list_symbol，漏了这里）。
    if c.startswith(("hk", "us")):
        return c
    if c.startswith(("sh", "sz")):
        return c
    # v1.2.65：裸代码的交易所判定统一走 sf_utils.market_prefix（唯一实现）。
    # 原兜底 `sh if c[0] in "69" else sz` 只认识股票代码，会把 5/1/2/7 开头的
    # 沪市基金与 ETF 一律判成深市（510300 → sz510300），接口永远返回空 →
    # 界面永久 "--"。无法判定时仍回退 "sz"（与旧兜底一致，含空串/None 入参）。
    pfx_raw = c
    # v1.3.42 第4轮走查：与 normalize_code 同口径，裸的北交所 4 位老代码
    # （8307 / 4300 这类）先补 0 到 6 位再判交易所。此前只有带 bj 前缀的走补零，
    # 裸 "8307" 直接拿 4 位去查 market_prefix → 判定失败 → 兜底成 "sz8307"，
    # 北交所标的行情与分时全部取空，界面永久 "--"。
    if pfx_raw.isdigit() and len(pfx_raw) == 4 and pfx_raw.startswith(("4", "8")):
        pfx_raw = pfx_raw + "00"
    pfx = market_prefix(pfx_raw[:6]) if pfx_raw.isdigit() else ""
    # 返回补零后的代码（8307 → bj830700），否则只改了判定、没改结果。
    return (pfx or "sz") + pfx_raw

def price_decimals(code):
    """价格展示小数位（主备源归一用）。

    v1.2.65 第1轮走查：腾讯与新浪对**同一只**标的返回的小数位不同 ——
    实测 2026-09-02：
        sh600519  腾讯 "1297.50"   新浪 "1297.500"
        bj920001  腾讯 "12.10"    新浪 "12.100"
    显示层 _anim_decimals 是从价格字符串反推的（见 FloatWindow._apply_quote），
    于是主源失败自动切备用源时，价格文本长度突变 → 自适应字号随之跳动，
    用户看到价格数字「闪一下变大/变小」，且切换期间一直保持错的宽度。
    在数据源出口按品种把小数位统一即可根治。

    规则（A 股报价惯例）：基金/ETF/LOF/可转债 3 位，B 股 3 位，港股 3 位，其余股票 2 位。
    无法判定时回落 2 位（A 股最常见形态，也是最保守的选择）。

    v1.2.71：补港股 3 位 —— 港股报价常规为 3 位小数（如 610.500），此前
    与美股一并落到 "非 6 位纯数字 → 2 位" 的兜底，_norm_price 会把
    610.500 截成 610.50，港股价格精度被削掉一位。
    """
    c = (code or "").strip().lower()
    if c.startswith("hk"):
        return 3      # 港股：3 位小数报价
    if c.startswith("us"):
        return 2      # 美股：2 位
    digits = c[2:] if c[:2] in ("sh", "sz", "bj") else c
    if len(digits) != 6 or not digits.isdigit():
        return 2
    if digits[0] in ("5", "1"):
        return 3      # 沪市基金/ETF(50/51/56/58)、深市基金/ETF(15/16/18)与可转债(12)
    if digits.startswith("920"):
        return 2      # 北交所新段是**股票**（2 位）—— 必须早于下方 9 开头的 B 股判定，
                      # 否则 920xxx 会被误判成沪市 B 股而显示成 3 位
    if digits[0] == "9" or digits.startswith("200"):
        return 3      # B 股：沪市 900(美元计价)、深市 200(港币计价)，报价 3 位
    return 2


def _norm_price(code, s):
    """把价格字符串的小数位归一到 price_decimals(code)；非数值原样返回。

    只改**字符串形态**，数值不变 —— 下游 float(data["price"]) 结果一致。
    """
    try:
        v = float(s)
        if not math.isfinite(v):
            return s
        return ("%%.%df" % price_decimals(code)) % v
    except (TypeError, ValueError):
        return s


def _is_finite_str(s):
    """字符串是否可解析为有限浮点数；解析失败或非有限返回 False。
    两源可选数值字段共用此校验；只判有效性，不改原始文本与展示精度。"""
    try:
        return math.isfinite(float(s))
    except (ValueError, TypeError):
        return False


def _optional_quote_field(fields, index):
    """读取源协议中的可选数值字段；缺失/空/非法/非有限返回空串。

    fields 来自响应字符串的 split，index 是协议中的非负字段序号。
    有效值保留空白、正负号和尾零，避免容错读取顺带改变行情展示文本。
    """
    if index < len(fields):
        value = fields[index]
        if value.strip() and _is_finite_str(value):
            return value
    return ""


def _sina_list_symbol(code):
    """新浪行情列表代码映射：A 股直接用 sh/sz/bj 前缀；港股 hk00700；美股 gb_代码(小写)。
    v1.2.69 修复：旧实现把归一化代码直接拼进新浪 URL，且用 code[-6:] 截取，
    对港股 hk00700(7 字符)截成 k00700、对美股 usaapl 拼成裸串，新浪无法识别 →
    主源故障时美股/港股回退必失败、整条行情丢失。此处按市场正确转换。"""
    c = (code or "").lower()
    if c.startswith("hk"):
        return c                      # hk00700
    if c.startswith("us"):
        return "gb_" + c[2:]          # usaapl -> gb_aapl
    return c                          # sh600519 / sz000001 / bj...


def fetch_quote_fallback(code):
    """备用行情源（新浪 hq.sinajs.cn）：主源异常/网络失败时降级。
    返回与 _fetch_quote_primary 同构 dict；解析失败抛 QuoteDataError。
    新浪返回 GBK 编码，http_get 默认 gbk=True 适用。"""
    data = http_get(QUOTE_URL_FALLBACK % _sina_list_symbol(code),
                    headers={"User-Agent": "Mozilla/5.0", "Referer": SINA_REFERER})
    if '="' not in data:
        raise QuoteDataError("fallback malformed payload: %r" % data[:80])
    payload = data.split('="', 1)[1].rstrip('";\n')
    fields = payload.split(",")
    # 新浪字段：0名称 1今开 2昨收 3现价 4最高 5最低 ...
    if len(fields) < 6 or not fields[0].strip() or not fields[3].strip():
        raise QuoteDataError("fallback field count short or empty name/price")
    for idx in (2, 3):
        try:
            value = float(fields[idx])
            if not math.isfinite(value):
                raise ValueError("non-finite")
        except ValueError:
            raise QuoteDataError("fallback bad numeric field %d: %r" % (idx, fields[idx]))
    code_out = code                   # 用归一化后的完整代码（含市场前缀），供涨跌停幅度判定
    try:
        price, pre_close = float(fields[3]), float(fields[2])
        if pre_close > 0:
            change = price - pre_close
            pct = change / pre_close * 100
        else:
            # v1.3.49：昨收为 0（新股上市首日/代码异常/部分板块非交易时段）时涨跌幅
            # 是**未知的**，置空串由下游 _apply_quote 兜底显示 "--" —— 绝不能显示
            # 0.00% 冒充平盘（资深股民会误判成平盘）。现价仍是真实有效的，整条保留。
            change, pct = "", ""
            log_line("quote fallback pre_close<=0 code=%s pre=%r price=%r —— 涨跌幅未知，置空显示 --"
                     % (code_out, fields[2], fields[3]), "WARNING")
    except Exception:
        # 计算异常沿用历史零值兜底；正常路径昨收非正仍返回空串，两者不混用。
        change, pct = 0.0, 0.0

    change_text, pct_text = str(change), str(pct)

    return QuoteData({
        # v1.2.65：price 经 _norm_price 归一小数位（新浪对股票返回 3 位，与腾讯的
        # 2 位不一致，切源时价格文本长度会突变导致字号抖动）。
        "name": fields[0], "code": code_out, "price": _norm_price(code, fields[3]),
        "pre_close": fields[2],
        "change": change_text if _is_finite_str(change_text) else "",
        "pct": pct_text if _is_finite_str(pct_text) else "",
        "high": _optional_quote_field(fields, 4), "low": _optional_quote_field(fields, 5),
    }, timestamp=(fields[30] + " " + fields[31]) if len(fields) > 31 else "")

def fetch_quote(code):
    """行情拉取：主源（腾讯）→ 异常/网络失败自动切备用源（新浪）。
    格式错误（非网络）连续达到阈值输出醒目 ERROR 告警（4.2）；主源成功则复位计数。
    6.0 v1.2.0：记录最近一次成功数据源（tencent/sina），供诊断导出。

    v1.2.65 第1轮走查：入口处统一 normalize_code —— 分时路径（_fetch_minute_primary）
    一直有 _tencent_code 兜底，但**主行情路径没有**：QUOTE_URL 直接 %s 拼原始 code。
    腾讯接口自 2026-08 起对裸代码返回 code param error，于是配置里存裸代码时
    （编辑器允许直接输入 6 位数字）行情主源必然失败、只靠新浪备用源兜底。
    这里补上与分时路径一致的归一化；非 A 股形式（usAAPL / hk00700）经
    normalize_code 原样返回，行为不变。
    """
    global _LAST_QUOTE_SOURCE
    code = normalize_code(code or "")
    try:
        q = _fetch_quote_primary(code)
        _reset_format_error("quote")
        _reset_degraded_source()          # 主源活着：降级连击归零
        _LAST_QUOTE_SOURCE = "tencent"
        return q
    except QuoteDataError as exc:
        _bump_format_error("quote", "QUOTE", code, exc)
        try:
            q = fetch_quote_fallback(code)
            log_line("quote fallback(tencent->sina) ok code=%s" % code, "WARNING")
            _LAST_QUOTE_SOURCE = "sina"
            # v1.2.69：备源成功即视为有效数据，复位格式错误计数，避免主源偶发
            # 格式异常时每 3 次刷一条误导性的"数据源格式变更"错误（实际 sina 正常）。
            _reset_format_error("quote")
            # v1.4.59（S3）：但"有数据"不等于"没坏"——降级这件事单独记账。
            _note_degraded_source(code, exc)
            return q
        except Exception:
            # 备源同样失败时抛回**主源**异常：调用方（日志/诊断）看到的是首个真实
            # 故障原因，而不是被备源的连带失败掩盖。
            raise exc
    except Exception as exc:
        # 网络异常（主源不可达）也尝试备用源：单源故障可降级，界面不至于长期"网络异常"
        try:
            q = fetch_quote_fallback(code)
            log_line("quote net-fail fallback(tencent->sina) ok code=%s" % code, "WARNING")
            _LAST_QUOTE_SOURCE = "sina"
            # v1.2.69：同上，备源成功复位格式错误计数
            _reset_format_error("quote")
            _note_degraded_source(code, exc)
            return q
        except Exception:
            # 同上：备源也失败时抛回主源异常，保留首个真实故障原因。
            raise exc

def _fetch_quote_primary(code):
    """腾讯主源：v_sh600519="1~贵州茅台~600519~..." 波浪分节格式，拉回并组装行情字典。

    字段索引（腾讯接口的分节位，改动 URL 版本时按 r_"v_sh..." 实包重核一遍）：
        fields[1] 名称   fields[2] 代码   fields[3] 现价   fields[4] 昨收
                  —— 首部必填四项（见下方强校验）
        fields[31] 涨跌额（元）   fields[32] 涨跌幅（%）
        fields[33] 今日最高   fields[34] 今日最低
        fields[36] 成交量（接口原值，工程内只判 >0）
        fields[47] 涨停价   fields[48] 跌停价
    出口两件归一化都在这里做：price 过 _norm_price 按 price_decimals(code) 统一小数位
    （模块 docstring 的字段契约），尾部扩展字段经 _optional_quote_field 容错读取。
    抛 QuoteDataError 表示"不是网络问题、是内容不像行情"，由上层计入格式错误连击。
    """
    data = http_get(QUOTE_URL % code)
    if '="' not in data:
        raise QuoteDataError("malformed payload: %r" % data[:80])
    payload = data.split('="', 1)[1].rstrip('";\n')
    fields = payload.split("~")
    # 3.1 北交所等板块字段容错：不同板块返回字段数不一致，仅强校验首部必填
    # 字段（名称/代码/现价/昨收），尾部扩展字段按存在性容错，缺失时置空串。
    if len(fields) < 5 or not fields[1] or not fields[2]:
        raise QuoteDataError("field count too short or empty name/code")
    for idx in (3, 4):
        if not fields[idx].strip():
            raise QuoteDataError("missing key price field %d" % idx)
        try:
            value = float(fields[idx])
            if not math.isfinite(value):
                raise ValueError("non-finite")
        except ValueError:
            raise QuoteDataError("bad numeric field %d: %r" % (idx, fields[idx]))

    # 4.1 数据源字段语义自检：change≈现价-昨收、pct≈change/昨收*100。
    # 偏差显著时记 WARNING（不抛错，避免接口偶发扰动中断行情展示）。
    try:
        price, pre_close = float(fields[3]), float(fields[4])
        change_text = _optional_quote_field(fields, 31)
        if change_text and pre_close != 0:
            if abs(float(change_text) - (price - pre_close)) > 0.02:
                log_line("quote field semantic warn: code=%s change=%s vs price-pre=%.3f"
                         % (code, change_text, price - pre_close), "WARNING")
    except Exception:
        # 语义自检仅作附加诊断；自检异常不改字段，也不应中断行情展示。
        pass
    return QuoteData({
        # v1.2.65：同 fallback —— 出口处归一小数位，保证主备源宽度一致
        "name": fields[1], "code": fields[2], "price": _norm_price(code, fields[3]),
        "pre_close": fields[4],
        "change": _optional_quote_field(fields, 31), "pct": _optional_quote_field(fields, 32),
        "high": _optional_quote_field(fields, 33), "low": _optional_quote_field(fields, 34),
        # 1.1 v1.2.0：成交量/涨停价/跌停价（腾讯字段 36/47/48），容错读取，缺失置空串
        "volume": _optional_quote_field(fields, 36),
        "limit_up": _optional_quote_field(fields, 47), "limit_down": _optional_quote_field(fields, 48),
    }, timestamp=fields[30] if len(fields) > 30 else "")

def _beijing_now():
    """固定按北京时间（UTC+8，中国无夏令时）取当前时间，避免依赖本机时区/时钟。
    v1.2.69：开收盘判定改用北京时间，出国或电脑时钟不准也不会误判。"""
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))


def _is_trading_time(now=None):
    """是否处于 A 股行情活跃时段，含开盘竞价及开盘前等待阶段。"""
    if now is None:
        now = _beijing_now()
    if is_holiday(now.date()):
        return False
    if now.weekday() >= 5:
        return False
    hm = now.hour * 60 + now.minute
    return (9 * 60 + 15 <= hm <= 11 * 60 + 30) or (13 * 60 <= hm <= 15 * 60)


def auction_status(now=None):
    """交易日的集合竞价阶段；09:25 后保留实时刷新但不冒充竞价中。"""
    if now is None:
        now = _beijing_now()
    if now.weekday() >= 5 or is_holiday(now.date()):
        return None
    hm = now.hour * 60 + now.minute
    if 9 * 60 + 15 <= hm < 9 * 60 + 25:
        return "集合竞价"
    if 9 * 60 + 25 <= hm < 9 * 60 + 30:
        return "竞价结束"
    if 14 * 60 + 57 <= hm < 15 * 60:
        return "集合竞价"
    return None

# 交易/非交易状态**唯一可能翻转**的时刻（北京时间，单位：当日第几分钟）。
# 与 _is_trading_time 严格同源：它只看 (日期, 时, 分)，所以状态只会在某个
# "分钟的起点"改变，一秒都不会差。
#   0    跨天 —— 周末/节假日判定会变
#   555  09:15 开盘竞价开始，高频刷新开始
#   565 / 570  09:25 竞价结束 / 09:30 连续竞价开始
#   691  11:31 进午休（hm <= 11*60+30 失效；11:30 整仍算盘中）
#   780  13:00 下午开盘
#   897 / 900  14:57 收盘竞价开始 / 15:00 撮合结束
#   901  15:01 收盘（15:00 整仍算盘中）
_MIDNIGHT_FLIP = 0                # 午夜回退档：跨日后 hm 从大数跳回 0，状态机靠它翻转
_INTRADAY_FLIPS = (555, 565, 570, 691, 780, 897, 900, 901)


def seconds_to_session_change(now=None):
    """距下一个"交易时段状态可能改变"的时刻还有多少秒。

    供后台线程按需休眠使用：单次睡眠不得超过它，否则 09:30 开盘时线程还在睡，
    表现为开盘头几十秒行情不动 —— 这正是把 0.1 秒死节拍改成按需休眠时必须
    补上的那块拼图（盘后间隔 3600 秒，若不卡边界就会睡过头）。

    异常时返回 0.0：调用方据此退回"固定 0.1 秒节拍"的旧行为，宁多醒一次，
    也绝不因为算不出边界而漏掉开盘。
    """
    try:
        if now is None:
            now = _beijing_now()
        best = None
        # 只需看今天和明天：明天的 00:00 必然 > now，候选一定找得到。
        for day_off in (0, 1):
            d = (now + datetime.timedelta(days=day_off)).date()
            flips = [_MIDNIGHT_FLIP]
            if d.weekday() < 5 and not is_holiday(d):
                flips += list(_INTRADAY_FLIPS)
            day_start = datetime.datetime.combine(d, datetime.time(0, 0),
                                                  tzinfo=now.tzinfo)
            for m in flips:
                delta = (day_start + datetime.timedelta(minutes=m) - now).total_seconds()
                if delta > 0 and (best is None or delta < best):
                    best = delta
        return 0.0 if best is None else best
    except Exception:
        return 0.0


def market_status(now=None):
    """返回休市或竞价阶段的 (提示文案, 颜色)；连续竞价中返回 None。
    模块级单一实现，FloatWindow._market_status 复用本函数，避免双份逻辑漂移。
    v1.2.69：改用北京时间，并识别法定节假日（非周末休市日）。"""
    if now is None:
        now = _beijing_now()
    if is_holiday(now.date()):
        return "休市", FG_DIM
    if now.weekday() >= 5:
        return "休市", FG_DIM
    hm = now.hour * 60 + now.minute
    auction = auction_status(now)
    if auction:
        return auction, FG_DIM
    if hm < 9 * 60 + 30:
        return "未开盘", FG_DIM
    if 9 * 60 + 30 <= hm <= 11 * 60 + 30:
        return None
    if hm < 13 * 60:
        return "午间休市", FG_DIM
    if hm <= 15 * 60:
        return None
    return "已收盘", FG_DIM

def _board_rate(code, name):
    """按代码板块/名称返回涨跌停幅度（百分比）；无法识别返回 0（不兜底）。
    规则（3.1 v1.2.27）：北交所(bj前缀/4/8/920开头)±30%、科创688/689、创业300/301±20%、
    主板±10%；ST 的 5% 仅对普通主板生效——创业/科创/北交所的 ST 仍按板块幅度。
    判断依据为代码前缀，稳而不误判：4/8/920 开头仅北交所、688/689 仅科创板、
    300/301 仅创业板，均无其他板块冲突。"""
    c = (code or "").strip().lower()
    digits = c[2:] if c[:2] in ("sh", "sz", "bj") else c
    if not digits.isdigit():
        return 0.0
    if c.startswith("bj") or digits.startswith(("4", "8", "920")):
        return 30.0
    if digits.startswith(("688", "689", "300", "301")):
        return 20.0
    n = (name or "").upper()
    if "ST" in n:
        return 5.0
    return 10.0


def _estimate_limit_prices(q):
    """行情源未提供涨跌停价时，按代码板块/名称本地估算涨停/跌停价。"""
    try:
        pre = float(q.get("pre_close") or 0)
    except (AttributeError, TypeError, ValueError):
        return 0.0, 0.0
    if pre <= 0 or not math.isfinite(pre):
        return 0.0, 0.0
    rate = _board_rate(q.get("code"), q.get("name"))
    if rate <= 0:
        return 0.0, 0.0
    decimals = price_decimals(q.get("code"))
    # 加一个远小于最小精度单位的小量（decimals=2 时为 1e-5）：把浮点乘法误差
    # （如 10.0*1.1=11.000000000000002）顶过进位线，避免四舍五入反而小一个价位；
    # 量级远小于半个最小单位，不会把真值抬高一档。
    up = round(pre * (1 + rate / 100.0) + 10 ** (-(decimals + 3)), decimals)
    down = round(pre * (1 - rate / 100.0) + 10 ** (-(decimals + 3)), decimals)
    return up, down


def official_limit_prices(q):
    """返回行情源明确提供的有效涨跌停价；字段缺失时返回 0。

    目标价输入是否允许越界必须只相信行情源的明确数据。代码板块估算适合展示
    状态，但无法覆盖新股上市初期等特殊规则，不能把估算值当作硬性拦截线。
    """
    if not isinstance(q, dict):
        return 0.0, 0.0

    def _valid(v):
        try:
            value = float(v)
            return value if value > 0 and math.isfinite(value) else 0.0
        except (TypeError, ValueError):
            return 0.0

    return _valid(q.get("limit_up")), _valid(q.get("limit_down"))


def limit_prices(q):
    """返回行情的有效涨跌停价；官方字段缺失时复用本地板块估算展示状态。"""
    up, down = official_limit_prices(q)
    if up <= 0 or down <= 0:
        estimated_up, estimated_down = _estimate_limit_prices(q)
        up = up or estimated_up
        down = down or estimated_down
    return up, down


def quote_status(q, now=None):
    """1.1 v1.2.0：判定股票当前状态徽标文案。
    返回 "涨停"/"跌停"/"停牌"/None（None=正常，不显示徽标）。
    - price >= limit_up > 0 → 涨停；price <= limit_down > 0 → 跌停。
    - 非集合竞价的交易时段，price/pre_close/volume 任一非正且至少有一项有效的
      原始涨跌停价或成交量为正时判停牌；本地估算不参与停牌判定。
    - 原始涨跌停价缺失时，涨跌停徽标可用板块估算；备用源字段不足不会误报停牌。"""
    if not isinstance(q, dict):
        return None
    try:
        price = float(q.get("price") or 0)
        pre_close = float(q.get("pre_close") or 0)
    except (TypeError, ValueError):
        return None

    def _f(v):
        try:
            x = float(v)
            return x if math.isfinite(x) else 0.0
        except (TypeError, ValueError):
            return 0.0

    limit_up_raw = _f(q.get("limit_up"))
    limit_down_raw = _f(q.get("limit_down"))
    volume = _f(q.get("volume"))
    # 停牌判定仅在交易时段生效（非交易时段 price=0 属正常收盘态，不误报停牌）
    if _is_trading_time(now) and not auction_status(now):
        if price <= 0 or pre_close <= 0 or volume <= 0:
            # 有任一涨跌停价/成交量字段"有效且>0"时才算停牌候选；字段为 0（腾讯
            # 非交易时段/刚开盘回 0 占位）或缺失（备用源无字段）均跳过，避免 0 值
            # 误判停牌（v1.2.22 修复）。真停牌时 limit_up 仍为有效价，可正常识别。
            # 3.1 v1.2.27：停牌判定仅使用行情源原始字段（limit_up_raw/limit_down_raw），
            # 本地板块估算值不参与，避免备用源无涨跌停字段时把正常股票误判"停牌"。
            if limit_up_raw > 0 or limit_down_raw > 0 or volume > 0:
                return "停牌"
    # 涨停/跌停：优先用行情源字段；两者均缺失（如新浪备用源无涨跌停字段）时，
    # 用本地板块估算兜底（3.1 v1.2.27：北交所30% / 主板ST 5% / 科创20% / 主板10%）。
    limit_up, limit_down = limit_prices(q)
    if limit_up > 0 and price > 0 and price >= limit_up:
        return "涨停"
    if limit_down > 0 and price > 0 and price <= limit_down:
        return "跌停"
    return None

class MinutePoints(list):
    """List-compatible minute points carrying the source day, not the receipt day."""

    def __init__(self, points=(), day=""):
        super().__init__(points)
        self.day = day


def _minute_source_day(value):
    if not isinstance(value, str):
        return ""
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return ""


def _ordered_minute_points(points):
    # Duplicate minutes use the last observation; connected lines require time order.
    unique = {point[0].replace(":", ""): point for point in points}
    points[:] = [unique[key] for key in sorted(unique)]
    return points


def fetch_minute_fallback(code):
    """备用分时源（东财 trends2）：主源异常/网络失败时降级。
    返回与 _fetch_minute_primary 同构的 points [(time, price, avg), ...]；
    东财 trends 行：时间,开,收,高,低,量,额,均价（收=parts[2]，均价=parts[7]）。"""
    secid = _secid(code)
    j = json.loads(http_get(MINUTE_URL_FALLBACK % secid, gbk=False,
                            timeout=MINUTE_HTTP_TIMEOUT, retries=MINUTE_HTTP_RETRIES))
    d = j.get("data") or {}
    trends = d.get("trends") or []
    # v1.2.72 第6轮走查：备用源也必须以北京时间裁剪。此前主源已改为
    # _beijing_now()，备用源却仍读本机时间；系统时区不在 UTC+8 时，北京盘中会
    # 错判为休市/前一日，分时图可能保留昨日全量或被错误裁短。
    now = _beijing_now()
    cur_only = _is_trading_time(now)
    today_strs = {now.date().strftime("%Y-%m-%d")}
    open_min = 9 * 60 + 30
    cur_min = now.hour * 60 + now.minute
    close_min = 15 * 60
    points = MinutePoints()
    for row in trends:
        parts = row.split(",")
        if len(parts) < 8:
            continue
        t_part = parts[0].strip()
        # 东财时间形如 "2026-08-21 09:30"，取 HH:MM 与主源格式对齐
        hm_part = t_part.split()[-1] if " " in t_part else t_part
        source_day = _minute_source_day(t_part.split()[0]) if " " in t_part else ""
        if " " in t_part and (not source_day or source_day > now.date().isoformat()):
            continue
        # 交易时段严格校验日期为当日，防 fallback 返回昨日/缓存数据画错进度
        if cur_only and " " in t_part:
            if t_part.split()[0] not in today_strs:
                continue
        hm = hm_part.replace(":", "")
        if len(hm) != 4 or not hm.isdigit() or int(hm[:2]) > 23 or int(hm[2:]) > 59:
            continue
        try:
            m = (int(hm[:2]) * 60) + int(hm[2:4])
        except Exception:
            continue
        # 与主源一致：交易时段上限 min(cur_min, close_min) 防 15:00 后盘后点被保留；
        # 非交易时段（cur_only=False）固定 15:00 上限保留最近交易日完整分时（v1.2.44）
        upper = min(cur_min, close_min) if cur_only else close_min
        if m < open_min or m > upper:
            continue
        try:
            price = float(parts[2])
            if not math.isfinite(price) or price <= 0:
                continue
            avg = float(parts[7]) if parts[7].strip() else None
            if avg is not None and (not math.isfinite(avg) or avg <= 0):
                avg = None
        except Exception:
            continue
        # 时间串保持东财原始 "HH:MM" 格式（v1.2.44 数据形状约定，见 test_v144）；
        # 渲染层 _chart_minute_offset 已做去冒号防御归一，两源格式差异不影响 X 轴换算。
        if source_day < points.day:
            continue
        if source_day > points.day:
            points.clear()
            points.day = source_day
        points.append((hm_part, price, avg))
    return _ordered_minute_points(points)

def fetch_minute(code):
    """分时拉取：主源（腾讯）→ 异常或盘中结构性空结果自动切备用源（东财）。

    v1.2.72 第6轮走查：原逻辑只在主源**抛异常**时降级；但主源局部缺数、标的
    键缺失、盘中回传昨日日期时会合法返回空列表，公共入口直接把空列表交给界面，
    即使东财备用源有数据，分时图仍是空白。盘中空分时不是可展示的成功结果，
    因而统一纳入备用源路径；非交易时段保留空列表语义，避免拿不到近期数据时
    无谓触发网络请求。

    成功走的是主源还是备用源，就地落账到 `get_last_minute_source()`（诊断导出
    读取）：行情侧有 get_last_quote_source，分时侧此前没有源追踪 —— "分时不动了"
    时远程分不清是主源坏了还是两源都坏。
    """
    global _LAST_MINUTE_SOURCE
    primary_empty = False
    try:
        pts = _fetch_minute_primary(code)
        if pts or not _is_trading_time():
            _reset_format_error("minute")
            if pts:
                _LAST_MINUTE_SOURCE = "primary"
            return pts
        # 主源结构完整但盘中没有点集：这是降级条件，不是接口格式坏了，不能
        # 累加 FORMAT ERROR（否则备用源正常时仍会把日志刷成"接口格式变更"）。
        primary_empty = True
        raise QuoteDataError("minute primary returned empty points during trading")
    except Exception as exc:
        if not primary_empty:
            _bump_format_error("minute", "MINUTE", code, exc)
        try:
            pts = fetch_minute_fallback(code)
            _reset_format_error("minute")
            if pts:
                _LAST_MINUTE_SOURCE = "fallback"
                log_line("minute fallback(tencent->eastmoney) ok code=%s" % code, "WARNING")
            return pts
        except Exception:
            raise exc


_LAST_MINUTE_SOURCE = ""


def get_last_minute_source():
    """最近一次分时成功回包走的源：'primary' / 'fallback' / ''（还没成功过）。

    只在成功那一刻改写，失败不清空 —— 与 get_last_quote_source 同一口径：
    "最后一次成功靠谁"正是排障要的答案。"""
    return _LAST_MINUTE_SOURCE

def _fetch_minute_primary(code):
    """分时数据：交易时段仅保留当日开盘(9:30)~当前时刻的当日数据；
    非交易时段（休市/盘前/午休/盘后/周末）接受接口返回的最近交易日完整分时
    （9:30~15:00），使休市时悬浮窗中央仍能展示最新一日的走势图。"""
    url = MINUTE_URL % _tencent_code(code)
    j = json.loads(http_get(url, gbk=False, timeout=MINUTE_HTTP_TIMEOUT,
                            retries=MINUTE_HTTP_RETRIES))
    d = j.get("data", {}).get(_tencent_code(code), {})
    day = d.get("data", {}) if isinstance(d.get("data"), dict) else {}
    rows = day.get("data", []) or []
    date_str = day.get("date", "")
    # v1.2.71 第4轮走查：日期与时刻统一取北京时间（理由同 fetch_minute_fallback）。
    # 原用本地 date.today()/datetime.now()，与按北京时刻解释的 _is_trading_time
    # 混用两套时基：非 UTC+8 用户盘中会走到 `return []`（交易时段却拿到"非当日"
    # 数据）或把 cur_min 算成前一天的时刻，分时窗口被裁得七零八落。
    now = _beijing_now()
    today = now.date()
    source_day = _minute_source_day(date_str)
    if date_str and (not source_day or source_day > today.isoformat()):
        return []
    today_strs = {today.strftime("%Y%m%d"), today.strftime("%Y-%m-%d")}
    if date_str:
        if date_str in today_strs:
            cur_only = True            # 当日数据：仅保留 9:30~当前时刻
        elif _is_trading_time(now):
            return []                  # 交易时段却拿到非当日数据 → 丢弃（防昨日拼接）
        else:
            cur_only = False           # 非交易时段：接受最近交易日完整分时
    else:
        # 无日期标注时按当前时段推测：交易时段只留当日裁剪，非交易时段全量
        cur_only = _is_trading_time(now)
    open_min = 9 * 60 + 30              # 开盘 9:30
    cur_min = now.hour * 60 + now.minute
    close_min = 15 * 60                 # 收盘 15:00
    # 标定成交量单位：第3列累计成交量、第4列累计成交额(元)。腾讯行情对科创板(688)
    # 返回"股"，其余板块返回"手"；均价=成交额/成交量，单位"手"需再×100。
    # 用首行 price*vol≈amount 判定单位是否"股"，否则按"手"（unit_factor=100）。
    unit_factor = 100.0
    if rows:
        fp = rows[0].split(" ")
        if len(fp) >= 4:
            try:
                p0, v0, a0 = float(fp[1]), float(fp[2]), float(fp[3])
                # 5% 容差：接口金额字段本身有舍入，不能要求 price*vol 精确等于 amount。
                if v0 > 0 and a0 > 0 and abs(p0 * v0 - a0) / a0 < 0.05:
                    unit_factor = 1.0
            except Exception:
                pass
    points = MinutePoints(day=source_day)
    for r in rows:
        parts = r.split(" ")
        if len(parts) < 2:
            continue
        t = parts[0].replace(":", "")
        if len(t) != 4 or not t.isdigit() or int(t[:2]) > 23 or int(t[2:]) > 59:
            continue
        try:
            hm = int(t[:4])
        except Exception:
            continue
        m = (hm // 100) * 60 + (hm % 100)
        # 分时图只绘制 9:30~15:00（240 分钟）。上限按时段区分：
        #  - 交易时段/当日数据（cur_only=True）：min(cur_min, close_min)。关键：收盘后
        #    即使 date=今日（cur_only=True）也不能用"当前时刻" cur_min 作上限——否则
        #    腾讯返回的 15:00 后盘后分时点（15:06~15:30）会被保留，而 _chart_x 将它们
        #    全部 clamp 到 x=240，右端 20+ 点重叠成竖线，分时图显示为"乱七八糟"。
        #  - 非交易时段接受最近交易日完整分时（cur_only=False）：v1.2.44 修复 —— 此前
        #    统一用 min(cur_min, close_min)，盘前/上午休市/周末上午 cur_min 早于 15:00，
        #    会把最近交易日 9:30~15:00 的分时点全量过滤成空，导致未开盘期间分时图空白。
        #    此时必须固定 15:00（close_min）为上限，完整保留最近交易日走势。
        upper = min(cur_min, close_min) if cur_only else close_min
        if m < open_min or m > upper:
            continue
        try:
            price = float(parts[1])
            if not math.isfinite(price) or price <= 0:
                continue
            if len(parts) >= 4:
                vol = float(parts[2])      # 累计成交量（截至该分钟，非增量，勿累加）
                amount = float(parts[3])   # 累计成交额（元）
                avg = (amount / (vol * unit_factor)) if vol > 0 else price
                if not math.isfinite(avg) or avg <= 0:
                    avg = price
            else:
                avg = None
            points.append((parts[0], price, avg))
        except Exception:
            continue
    return _ordered_minute_points(points)

def eastmoney_search_bj(keyword, count=8):
    """东财 suggest 搜索兜底，仅返回北交所(京A/_TB)股票，代码统一为 bj 前缀。
    腾讯 smartbox 搜索接口不收录北交所，故北交所替换须走本函数补充。
    v1.2.28：token 设置功能已移除，固定使用内置默认 token 构造 URL；
    封禁特征跟踪（mark_fallback_token_banned）一并移除，HTTP 4xx 时直接返回空。"""
    url = EASTMONEY_SEARCH_URL % (urllib.parse.quote(keyword), EASTMONEY_TOKEN_DEFAULT, str(count))
    try:
        # v1.3.65：超时由 6s 收紧到 SEARCH_BJ_TIMEOUT(3s)、重试 0 次——
        # 本函数只是北交所兜底，失败不阻断主搜索，没必要长等。
        raw = http_get(url, gbk=False, timeout=SEARCH_BJ_TIMEOUT, retries=0,
                       headers={"User-Agent": "Mozilla/5.0",
                                "Referer": "https://quote.eastmoney.com/"})
        j = json.loads(raw)
        rows = (j.get("QuotationCodeTable") or {}).get("Data") or []
        out = []
        for r in rows:
            if r.get("SecurityTypeName") == "京A" or r.get("MarketType") == "_TB":
                code = str(r.get("Code", "")).strip()
                if code:
                    out.append(("bj" + code, r.get("Name") or code))
        return out
    except Exception:
        # HTTP 4xx（401/403/429 等认证/限流）→ 返回空，跳过东财搜索。
        # v1.2.48(B1)：状态码经 sf_network.get_last_http_status() 实时读取 ——
        # 直接 import _LAST_HTTP_STATUS 拿到的是导入时快照（恒 None），4xx 判别
        # 永不生效；网络异常（连接失败/超时）时状态码为 None，同样返回空不区分。
        status = get_last_http_status()
        # 4xx（封禁/限流）与网络异常一律返回空：本函数只是北交所兜底补充，
        # 失败不阻断主搜索，此处也不需要区分失败原因。
        if isinstance(status, int) and 400 <= status < 500:
            return []
        return []

def search_stocks(keyword):
    """股票搜索编排器：缓存 → 本地确定匹配 → 腾讯主搜 ∥ 东财北交所兜底 → 本地模糊救场。

    顺序与理由：
      1. 空 keyword 直接 []（v1.2.69：不发无效网络请求）；
      2. 结果缓存命中（LRU+TTL，见 _SEARCH_CACHE 段注释）—— 同词短期重搜零网络往返；
      3. 本地字典"确定无疑"三类（代码精确/名称精确/唯一包含，实测 0.2~1.4ms，
         联网路径中位 56ms）—— 多结果的模糊词不在此接管，价值在相关性排序；
      4. 都未命中才联网：腾讯主搜（SEARCH_TIMEOUT / SEARCH_RETRIES）与东财北交所
         **并行**发起（v1.3.65：旧实现串行两趟相加，实测合计 187ms 均值）；腾讯结果
         不足 SEARCH_BJ_FALLBACK_MIN 时对东财做有界等待（SEARCH_BJ_TIMEOUT+0.5s）后合并；
      5. 联网 0 结果时本地模糊匹配救场（断网 / 接口抽风 / 腾讯不认的写法如 "sz000651" /
         腾讯不覆盖的北交所标的）—— 本地也没有才真的返回空。
    返回 [(code, name)]，code 带交易所前缀。任何一步失败都降级不抛错：搜索框不是
    行情主链路，宁可少结果也不能把异常抛到界面。
    """
    # 搜索输入统一去掉所有 Unicode 空白并折叠英文字母大小写。这样用户粘贴
    # " sh 600519 "、输入带换行的证券名称时，本地字典、网络请求和缓存使用
    # 同一个查询键，避免同义输入重复发请求或走不同的匹配路径。
    kw = _normalize_search_keyword(keyword)
    if not kw:
        return []                       # v1.2.69：空关键词直接不请求，避免无效网络调用
    # v1.3.65：结果缓存 —— 同一关键词短期重复搜索直接命中，0 网络往返。
    # 典型场景：搜索框连按回车、同一只股票反复搜索、结果列表上下翻看后重搜。
    _cached = _search_cache_get(kw)
    if _cached is not None:
        return list(_cached)
    # v1.3.66：本地字典 —— 代码精确 / 名称精确 / 唯一包含，这三类"确定无疑"的
    # 搜索直接本地出结果（实测 0.2~1.4ms，联网路径中位 56ms）。多结果的模糊词
    # 不在此接管：那种搜索的价值在相关性排序，继续走联网。
    try:
        _local = universe.match_confident(kw)
    except Exception:
        _local = []
    if _local:
        _search_cache_put(kw, _local)
        return list(_local)
    # 顺带触发后台字典刷新（字典缺失/过期时才真的发请求，且绝不阻塞本次搜索）
    try:
        universe.ensure_ready()
    except Exception:
        pass
    url = SEARCH_URL % urllib.parse.quote(kw)
    # v1.3.65：东财（北交所兜底）与腾讯主搜索**并行**。旧实现等腾讯返回后才
    # 串行发起东财，两趟耗时相加（实测合计 187ms 均值，东财单趟 66~225ms）。
    # 并行后总耗时 = max(腾讯, 东财)。连接池本身有 threading.Lock，可安全并发取用。
    bj_box = []
    bj_th = threading.Thread(
        target=_eastmoney_bj_worker,
        args=(kw, SEARCH_MAX_RESULTS, bj_box),
        name="sf-search-bj", daemon=True)
    try:
        bj_th.start()
    except Exception:                    # 线程起不来就退化为串行兜底（不阻断搜索）
        bj_th = None
    try:
        data = http_get(url, timeout=SEARCH_TIMEOUT, retries=SEARCH_RETRIES)
    except Exception:
        data = ""
    out = []
    seen = set()
    if '="' in data:
        payload = data.split('="', 1)[1].rstrip('";\n')
        for it in payload.split("^"):
            f = it.split("~")
            if len(f) >= 5 and f[4].startswith("GP-A") and f[0] in ("sh", "sz", "bj"):
                # 接口返回 \uXXXX 字面转义，解码为真实名称
                name = re.sub(r"\\u([0-9a-fA-F]{4})",
                              lambda m: chr(int(m.group(1), 16)), f[2])
                c = f[0] + f[1]
                if c not in seen:                      # v1.2.69：同源重复也去重
                    seen.add(c)
                    out.append((c, name))
                    # 弹窗最多展示固定数量；停止解析剩余的大响应，缩短搜索完成时间。
                    if len(out) >= SEARCH_MAX_RESULTS:
                        break
    # 腾讯结果未填满界面可展示的上限时，并入东财（北交所）候选。
    # v1.3.65：东财请求已提前并行发起，此处只做**有界等待 + 合并**，
    # 总耗时由"腾讯+东财"降为两者取大；兜底能力（北交所）与原实现一致。
    if len(out) < SEARCH_BJ_FALLBACK_MIN:
        if bj_th is not None:
            bj_th.join(timeout=SEARCH_BJ_TIMEOUT + 0.5)   # 有界等待，绝不无限阻塞
        for c, n in bj_box:
            if c not in seen:
                seen.add(c)
                out.append((c, n))
                if len(out) >= SEARCH_MAX_RESULTS:
                    break
    # v1.3.66：联网 0 结果时用本地字典救场（断网 / 接口抽风 / 腾讯不认的写法
    # 如 "sz000651"、以及腾讯不覆盖的北交所股票）。本地也没有才真的返回空。
    if not out:
        try:
            out = list(universe.match_fuzzy(kw, SEARCH_MAX_RESULTS))
        except Exception:
            out = []
    _search_cache_put(kw, out)
    return out


def _normalize_search_keyword(keyword):
    """归一化搜索词，保持中文语义并消除粘贴输入带来的格式差异。"""
    return "".join(str(keyword or "").split()).casefold()


def _eastmoney_bj_worker(kw, count, box):
    """并行 worker：取回北交所候选。异常一律吞掉 —— 兜底失败绝不阻断主搜索。"""
    try:
        box.extend(eastmoney_search_bj(kw, count=count) or [])
    except Exception:
        return


# ---------- v1.3.65：搜索结果缓存（LRU + TTL） ----------
# 缓存只存"代码 + 名称"映射，不含行情价，5 分钟内陈旧无业务影响。
# 测试环境一律不启用：多个用例会搜同一个关键词（如 "foo"），缓存会让后一个
# 用例拿到前一个用例的结果（实测把 test_full_tencent_results_skip_bj_request
# 的结果从 50 条变成 6 条）。与 sf_constants 的落盘路径隔离同款兜底。
_SEARCH_CACHE = collections.OrderedDict()   # kw -> (monotonic_ts, results)
_TEST_ENV = "unittest" in sys.modules and not getattr(sys, "frozen", False)


def _search_cache_get(kw):
    """取缓存：测试环境恒未命中；过期条目顺手弹出并返回 None；命中者移到队尾续命（LRU）。"""
    if _TEST_ENV:
        return None
    ent = _SEARCH_CACHE.get(kw)
    if ent is None:
        return None
    ts, res = ent
    if (time.monotonic() - ts) > SEARCH_CACHE_TTL:
        try:
            _SEARCH_CACHE.pop(kw, None)
        except KeyError:
            pass
        return None
    try:
        _SEARCH_CACHE.move_to_end(kw)
    except Exception:
        pass
    return res


def _search_cache_put(kw, res):
    """写缓存：测试环境一律不写（用例间会互相串结果）；超容量从队头（最旧）淘汰。"""
    if _TEST_ENV:
        return
    try:
        _SEARCH_CACHE[kw] = (time.monotonic(), list(res))
        _SEARCH_CACHE.move_to_end(kw)
        while len(_SEARCH_CACHE) > SEARCH_CACHE_MAX:
            _SEARCH_CACHE.popitem(last=False)
    except Exception:
        pass
