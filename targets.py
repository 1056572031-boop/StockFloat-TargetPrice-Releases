# -*- coding: utf-8 -*-
"""「目标价」功能：模型 / 状态机 / 生命周期 / 启动恢复（纯逻辑，可脱 Qt 单测）。

所有"当前时间"均经 sf_quote._beijing_now() 取得（北京时间 UTC+8），
绝不依赖本机时区/时钟；测试通过注入 now 参数验证判定，不触碰真实时钟。
"""
import math
from datetime import date, datetime, timedelta, timezone

from sf_constants import TARGET_DIR_ABOVE, TARGET_DIR_BELOW
from sf_calendar import next_trading_day, is_trading_day, is_trading_day_confirmed
from sf_quote import _beijing_now

STATE_PENDING = "pending"
STATE_HIT = "hit"
SIDE_ABOVE = "ABOVE"
SIDE_BELOW = "BELOW"

# 收盘后清空时刻（北京时间）；v6 D1：专用定时器按此时刻准时触发。
CLEAR_HOUR, CLEAR_MINUTE, CLEAR_SECOND = 15, 0, 30


def _beijing_tz():
    return timezone(timedelta(hours=8))


def _parse_session_date(sd):
    """"YYYY-MM-DD" → (y, m, d)；缺失 / 非字符串 / 格式错 / 日期不存在均返回 None。

    R33-2：原先 _clear_dt 直接 split+int，遇到脏数据抛 ValueError，
    会把整批目标价连坐清空（_init_targets 捕获异常后 _alerts = []）。
    """
    if not isinstance(sd, str):
        return None
    parts = sd.split("-")
    if len(parts) != 3:
        return None
    try:
        y, m, d = (int(x) for x in parts)
    except (TypeError, ValueError):
        return None
    try:
        datetime(y, m, d)
    except ValueError:
        return None  # 2026-02-30 之类
    return y, m, d


def _clear_dt(session_date):
    """session_date 当天的 15:00:30 北京时刻（tz-aware）；非法返回 None。"""
    parsed = _parse_session_date(session_date)
    if parsed is None:
        return None
    y, m, d = parsed
    return datetime(y, m, d, CLEAR_HOUR, CLEAR_MINUTE, CLEAR_SECOND, tzinfo=_beijing_tz())


def _valid_price(price):
    """现价是否可用于越线判定：必须是有限正数。

    R33-1：冷启动行情未到（_current_price() 返回 0.0）或行情异常（0 / None / NaN）
    时，用这些值判定会把「跌到 X」误判成已越过（0 <= X），导致一开机就误弹
    提醒并误开彩虹高亮。没有可信价格就不下结论。
    """
    try:
        p = float(price)
    except (TypeError, ValueError):
        return False
    # p != p 是判定 NaN 的惯用写法：NaN 与任何值（包括它自己）比较都为 False
    if p != p or p in (float("inf"), float("-inf")):
        return False
    return p > 0


def _valid_alert(a):
    """alert 结构是否完整可用于判定（合法 direction + 正有限 target）。"""
    if not isinstance(a, dict):
        return False
    if a.get("direction") not in (TARGET_DIR_ABOVE, TARGET_DIR_BELOW):
        return False
    try:
        t = float(a.get("target"))
    except (TypeError, ValueError):
        return False
    # 同 _valid_price：先按 NaN 判（t != t），再排 ±inf，最后才判正负
    if t != t or t in (float("inf"), float("-inf")):
        return False
    return t > 0


def _as_beijing(dt):
    """把 naive datetime 视作北京时间；None 原样返回。

    测试注入的时刻通常是"北京时间"的 naive datetime，直接和 tz-aware 的
    清空时刻比较会抛 TypeError，统一在此补齐时区。
    """
    if dt is None:
        return None
    if getattr(dt, "tzinfo", None) is None:
        return dt.replace(tzinfo=_beijing_tz())
    return dt


def _day_clear_dt(d):
    """某日期当天的 15:00:30 北京时刻。"""
    return datetime(d.year, d.month, d.day,
                    CLEAR_HOUR, CLEAR_MINUTE, CLEAR_SECOND, tzinfo=_beijing_tz())


def in_effect_window(alert, now):
    """alert 此刻是否处于"可判定触发"窗口（纯函数，可单测）。

    窗口 = 生效日当天 00:00 至 15:00:30（不含）。三种情况一律不判定：
      - 未来生效日：盘后/周末/节假日创建的次交易日目标，未生效不能提前触发；
      - 过去生效日：清空定时器漏触发或休眠跨过 15:00:30，此时行情是收盘后的
        缓存价，按它判定会"补发"一条本该随收盘一起作废的提醒；
      - session_date 缺失/不可解析：无效数据，安全方向是丢弃。
    """
    dt = _clear_dt(alert.get("session_date") if isinstance(alert, dict) else None)
    if dt is None:
        return False
    now = _as_beijing(now)
    if now is None:
        return False
    # 未来月份官方日历未到时，不以“按周末猜测”的日期触发提醒。
    # 等日历确认后再生效，可避免法定假日用收盘缓存价误触发。
    if is_trading_day_confirmed(dt.date()) is not True:
        return False
    return now.date() == dt.date() and now < dt


def _session_date_for(now):
    """给定北京时刻，返回目标价"生效日"（YYYY-MM-DD）。

    v1.2.79 修正：原先只问"现在是不是连续竞价时段"，于是
      - 开盘前（08:00）、午休（12:00）设目标价 → 被推到**下一个交易日**；
    但当天明明还没收盘，正确结果是当天。改为：
      - 当天是交易日且未到清空时刻（15:00:30）→ 当天（含盘前与午休）；
      - 当天是交易日但已过清空时刻        → 下一交易日；
      - 当天非交易日（周末/节假日） → 下一交易日。

    R45：判定基线由"整点 15:00"改为与清空时刻同源的 15:00:30，
    消除 15:00:00–15:00:29 这 30 秒"新目标归次日、旧目标当日才清"的空档。
    """
    now = _as_beijing(now)
    if now is None:
        return ""
    d = now.date()
    if is_trading_day(d) and now < _day_clear_dt(d):
        return d.strftime("%Y-%m-%d")
    return next_trading_day(d).strftime("%Y-%m-%d")


def roll_to_trading_day(session_date, max_roll=30):
    """生效日若不是交易日，顺延到下一个交易日（纯函数，可单测）。

    R47：跨年或未来月份的交易日历尚未缓存时，`is_holiday` 会降级成"非休市"，
    `_session_date_for` 于是可能返回一个实际休市的日期（例如 2026-12-31 晚上
    设的目标价落到 2027-01-01 元旦）。后果是当天全天无行情、15:00:30 被静默
    清空，用户一次提醒都收不到，且界面上看不出任何异常。

    这里做惰性纠正：只要生效日不是交易日就往后顺延，日历补齐后自动生效。
    上限 max_roll 次，避免日历数据异常时无限递推。
    """
    parsed = _parse_session_date(session_date)
    if parsed is None:
        return session_date
    d = date(*parsed)
    for _ in range(max(1, int(max_roll))):
        verdict = is_trading_day_confirmed(d)
        if verdict is True:
            return d.strftime("%Y-%m-%d")
        if verdict is None:
            # 日历未确认时保留原生效日并触发后台预取，不擅自顺延或清空。
            return session_date
        d = next_trading_day(d)
    return session_date


def roll_alerts_to_trading_day(alerts):
    """批量顺延非交易日生效日，返回 (alerts, changed)。"""
    out, changed = [], False
    for a in alerts or []:
        if not isinstance(a, dict):
            out.append(a)
            continue
        sd = a.get("session_date")
        new_sd = roll_to_trading_day(sd)
        if new_sd != sd:
            a = dict(a)
            a["session_date"] = new_sd
            changed = True
        out.append(a)
    return out, changed


def _fmt_target(t, decimals=2):
    """通知/列表用的目标价文本。

    R41：极端数值（手滑输入 1e308）用 "%.2f" 会展开成 300 多位数字，
    落到通知正文里就是一条几百字符的横幅。超过 20 位时改用科学计数法。
    """
    try:
        s = ("%%.%df" % int(decimals)) % float(t)
    except (TypeError, ValueError):
        return str(t)
    return s if len(s) <= 20 else "%.3g" % float(t)


def current_price_side(price, target, direction):
    """现价相对目标的位置：ABOVE / BELOW。"""
    # 用 >= / <= 而非 > / <：恰好触及目标价即算达成，不做"必须越过才算"的严格判定
    if direction == TARGET_DIR_ABOVE:
        return SIDE_ABOVE if price >= target else SIDE_BELOW
    return SIDE_BELOW if price <= target else SIDE_ABOVE


def is_crossed(price, target, direction):
    """现价是否已越过目标（触发条件）。"""
    want = SIDE_ABOVE if direction == TARGET_DIR_ABOVE else SIDE_BELOW
    return current_price_side(price, target, direction) == want


def validate_alert(direction, target):
    """校验单个 alert 输入合法性，返回 (ok, reason)。"""
    if direction not in (TARGET_DIR_ABOVE, TARGET_DIR_BELOW):
        return False, "direction 必须是 above/below"
    try:
        t = float(target)
    except (TypeError, ValueError):
        return False, "target 不是合法数字"
    if t != t or t in (float("inf"), float("-inf")):
        return False, "target 不是有限数字"
    if t <= 0:
        return False, "target 必须大于 0"
    return True, ""


def make_alert(direction, target, now=None):
    """构造一条 alert dict（含 session_date / state / last_side）。"""
    now = now or _beijing_now()
    return {
        "direction": direction,
        "target": float(target),
        "session_date": _session_date_for(now),
        "state": STATE_PENDING,
        "last_side": None,
    }


def recover_alerts(alerts, now, price, decimals=2):
    """启动恢复：过期丢弃 / 未过期保留 / 已越线补提醒。

    返回 (kept_alerts, reminders)；reminders 为需补发"已越过"通知的 dict 列表
    （含 direction/target/text，股票名由调用方拼接）。过期条目静默丢弃，不提醒。

    R33 加固：
      - 现价不可信（0 / None / NaN）时**不做越线判定**，保留 pending、
        last_side=None、不补提醒 —— 宁可等真实行情，也不误报；
      - 脏数据（缺字段 / direction 非法 / target 非正 / session_date 不可解析）
        逐条静默跳过，不再让一条坏数据把整批目标价连坐清空；
      - 与 evaluate_tick 共用 in_effect_window：只有**已生效**的目标才做越线判定。
        盘后设的次交易日目标若在这里被提前判成 HIT，就会被当成"已触发"而
        在它真正生效的当天不再提醒 —— 用户设了却永远收不到。
    """
    kept, reminders = [], []
    know_price = _valid_price(price)
    now = _as_beijing(now)
    for a in alerts:
        if not _valid_alert(a):
            continue
        dt = _clear_dt(a.get("session_date"))
        if dt is None or now >= dt:
            continue  # 过期 / 不可解析：静默丢弃
        a = dict(a)
        # 已触发状态是持久化的事实，重启后必须保留，不能按当前价格再次补发通知。
        # 只有 pending 条目才需要用当前价做一次恢复判定。
        if a.get("state") == STATE_HIT:
            a["state"] = STATE_HIT
        elif (know_price and in_effect_window(a, now)
              and is_crossed(price, a["target"], a["direction"])):
            a["state"] = STATE_HIT
            op = "涨到" if a["direction"] == TARGET_DIR_ABOVE else "跌到"
            reminders.append({"direction": a["direction"], "target": a["target"],
                              "text": "已%s %s（休市/停牌期间越过）" % (
                                  op, _fmt_target(a["target"], decimals))})
        else:
            a["state"] = STATE_PENDING
        a["last_side"] = (current_price_side(price, a["target"], a["direction"])
                          if know_price else None)
        kept.append(a)
    return kept, reminders


def evaluate_tick(alerts, price, now=None, decimals=2):
    """行情 tick：对每条 pending alert 判定是否越过目标。

    返回 (updated_alerts, fired)；fired 为触发列表（含 direction/target/price/text）。
    越线后立即置 hit，不重复触发（已 hit 的条目原样透传）。

    R33 加固：现价不可信（0 / None / NaN）时原样返回、不触发任何提醒 ——
    行情断流时宁可什么都不做，也不能把「跌到 X」误判成已跌到。

    R45 加固：now 可注入（便于确定性单测），且只有处于生效窗口内的条目才判定。
    此前只拦"未来生效日"，漏了"过去生效日"——定时器漏触发或休眠跨过 15:00:30 后，
    一条早该作废的目标会被盘后缓存价补发提醒。
    """
    if not _valid_price(price):
        return [dict(a) for a in (alerts or []) if isinstance(a, dict)], []
    now = _as_beijing(now) or _beijing_now()
    fired, out = [], []
    for a in alerts:
        if not isinstance(a, dict):
            continue
        a = dict(a)
        if (a.get("state") == STATE_HIT or not _valid_alert(a)
                or not in_effect_window(a, now)):
            out.append(a)
            continue
        if is_crossed(price, a["target"], a["direction"]):
            a["state"] = STATE_HIT
            a["last_side"] = current_price_side(price, a["target"], a["direction"])
            op = "涨到" if a["direction"] == TARGET_DIR_ABOVE else "跌到"
            fired.append({"direction": a["direction"], "target": a["target"],
                          "price": price,
                          "text": "已%s %s（现价 %s）" % (
                              op, _fmt_target(a["target"], decimals),
                              _fmt_target(price, decimals))})
        else:
            a["last_side"] = current_price_side(price, a["target"], a["direction"])
        out.append(a)
    return out, fired


def _same_slot(a, b):
    """两条 alert 是否占同一"槽位"：同方向 + 同价位。"""
    if not isinstance(a, dict) or not isinstance(b, dict):
        return False
    if a.get("direction") != b.get("direction"):
        return False
    try:
        return float(a.get("target")) == float(b.get("target"))
    except (TypeError, ValueError):
        return False


def drop_expired(alerts, now):
    """剔除已过清空时刻的条目（纯函数，可单测）。

    R39-1：合并会保留「实时」条目的旧 session_date。若那条其实已经过期
    （15:00:30 的定时器迟到/漏触发、或休眠跨过了该时刻），它既不会再被
    定时器清掉、也不会被重新武装，会一直挂在列表里、铃铛一直亮到重启。
    """
    return [a for a in (alerts or [])
            if isinstance(a, dict) and not expired(a, now)]


def merge_alerts(desired, live, now=None):
    """合并"期望目标价"与"实时目标价"（面板保存专用，纯函数可单测）。

    规则：同方向 + 同价位 → 保留实时条目（含 state/session_date/last_side）；
          其余 → 采用期望条目（新构造，session_date 为"生效那一刻"）。

    解决的问题：
      - 面板打开期间某条已触发（hit），保存时不得回退成 pending，否则下个 tick 重复提醒；
      - 未改动的条目不重新计算生效日，避免把旧目标重新"续期"；
      - 若期望与实时完全一致，返回与 live 相等的列表，调用方可据此跳过落盘。

    R38：期望里若出现重复槽位（同方向 + 同价位），只保留第一条 ——
    否则同一目标价会在列表里存两份，越线时连发两条一模一样的通知。
    """
    live = [a for a in (live or []) if isinstance(a, dict)]
    out = []
    for d in desired or []:
        if not isinstance(d, dict):
            continue
        match = None
        candidates = [l for l in live if _same_slot(d, l)]
        if candidates:
            match = candidates[0]
            if now is not None:
                # R46：live 里若存在同槽位的重复脏数据，第一条可能已过期。
                # 直接取它，随后 _set_targets 的 drop_expired 会把整条丢掉 ——
                # 用户点了「立即生效」，目标价凭空消失且没有任何提示。
                #
                # v1.3.42 第4轮走查：R46 只挑了"第一条未过期的"，找不到时仍
                # 回退到 candidates[0]（已过期那条）。于是「面板打开期间后台
                # 跨过 15:00:30」这条真实路径照旧丢目标价：live 同槽位全部过期，
                # 取出的仍是过期条目，drop_expired 随后整条清掉 —— 用户点保存，
                # 界面提示成功，目标价却没了。
                # 正确语义：live 里没有可用的同槽位条目时，就该用期望条目本身
                # （带着"生效那一刻"新算出的 session_date），而不是拖一条死条目回来。
                match = next((c for c in candidates if not expired(c, now)), None)
        item = dict(match) if match is not None else dict(d)
        if any(_same_slot(item, kept) for kept in out):
            continue
        out.append(item)
    return out


def expired(alert, now):
    """单条 alert 是否已过清空时刻（用于启动恢复/定时器武装判定）。

    session_date 缺失或不可解析一律视为已过期 —— 安全方向是丢弃，
    而不是让一条坏数据永久滞留并反复武装定时器。
    """
    dt = _clear_dt(alert.get("session_date") if isinstance(alert, dict) else None)
    if dt is None:
        return True
    # 未来月份日历未确认时，绝不按本地时钟清空；等待后台同步完成后再决定。
    if is_trading_day_confirmed(dt.date()) is None:
        return False
    now = _as_beijing(now) or _beijing_now()
    return now >= dt


# v1.2.95 第7轮走查：distance_pct 的入参校验漏了 ±inf ——
# 旧判断 `p == p（去 nan）+ p > 0 + t > 0` 会让 inf 通过。
# 于是 distance_pct(inf, 10) 得到 nan、distance_pct(10, inf) 得到 inf，
# 主窗目标价摘要就渲染出 "nan%" / "+inf%" 这类失真文本 ——
# 而这正是本函数 docstring 明确写着要避免的东西。改用 isfinite 一并拦住。
def distance_pct(price, target):
    """现价距目标价的百分比（正=还需涨/跌的幅度；负=已越过）。

    价格或目标价不可信（0 / None / NaN / ±inf / 非正）一律返回 None，调用方据此
    只显示价位、不显示百分比，避免出现 "+inf%" / "nan%" 这类失真文本。
    """
    try:
        p = float(price)
        t = float(target)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(p) and math.isfinite(t)) or p <= 0 or t <= 0:
        return None
    try:
        d = (t - p) / p * 100.0
    except (ZeroDivisionError, OverflowError):
        return None
    # 极端量级（p 极小、t 极大）仍可能算出 inf，同样按不可信处理
    return d if math.isfinite(d) else None


def nearest_pending(alerts, price):
    """从 pending 目标价里挑距现价最近的一条（用于主窗单行摘要）。

    已触发（hit）的条目不计入；价格不可信时返回 None；无 pending 返回 None。
    """
    best, best_d = None, None
    for a in alerts or []:
        if not isinstance(a, dict):
            continue
        if a.get("state") == STATE_HIT or not _valid_alert(a):
            continue
        d = distance_pct(price, a.get("target"))
        if d is None:
            continue
        ad = abs(d)
        if best_d is None or ad < best_d:
            best_d, best = ad, a
    return best


def bell_bubble_rows(alerts, price, decimals=2):
    """铃铛悬浮气泡的行数据（纯函数，可单测）：返回 (待触发行, 已触发行)。

    每行是 (is_up, 价位文本, 距现价百分比文本)：
    - 两组各自按目标价从高到低排，像一把价格梯子，不依赖设置的先后顺序；
    - 百分比只给待触发行，口径与目标价面板一致（带正负号、一位小数）；
      已触发行价格已越过目标，百分比没有意义，留空；
    - 现价不可信时 distance_pct 返回 None，百分比同样留空，不写 "--"；
    - 结构不合法的条目（缺方向、目标价非正/非有限）直接跳过，不让一条坏数据拖垮整块气泡。
    """
    fmt = "%%.%df" % int(decimals)
    pending, hit = [], []
    for a in alerts or []:
        if not _valid_alert(a):
            continue
        target = float(a.get("target"))
        is_up = a.get("direction") == TARGET_DIR_ABOVE
        if a.get("state") == STATE_HIT:
            hit.append((target, is_up, fmt % target, ""))
            continue
        d = distance_pct(price, target)
        pending.append((target, is_up, fmt % target,
                        "" if d is None else "%+.1f%%" % d))
    # sort 稳定：同价位时保持保存顺序，梯子不会在刷新间抖动
    pending.sort(key=lambda r: -r[0])
    hit.sort(key=lambda r: -r[0])
    return ([r[1:] for r in pending], [r[1:] for r in hit])


def build_target_label_text(alerts, price):
    """构造主窗右上角「目标价」单行标签文本（纯函数，可单测）。

    无目标价 → "设目标"（常驻入口）；
    有已触发 → "已触发 ↑%.2f"（留存至 15:00，P0-2）；
    仅有待触发 → "↑%.2f %+.1f%%"（最近一条的距目标价差%，P0-1）。
    价格不可信时退化为只显示价位，不显示百分比。
    """
    alerts = [a for a in (alerts or []) if isinstance(a, dict)]
    if not alerts:
        return "设目标"
    hit = [a for a in alerts if a.get("state") == STATE_HIT]
    if hit:
        a = hit[0]
        op = "↑" if a.get("direction") == TARGET_DIR_ABOVE else "↓"
        try:
            t = float(a.get("target", 0))
        except (TypeError, ValueError):
            t = 0.0
        return "已触发 %s%.2f" % (op, t)
    near = nearest_pending(alerts, price)
    if near is None:
        a = alerts[0]
        op = "↑" if a.get("direction") == TARGET_DIR_ABOVE else "↓"
        try:
            t = float(a.get("target", 0))
        except (TypeError, ValueError):
            t = 0.0
        return "%s%.2f" % (op, t)
    op = "↑" if near.get("direction") == TARGET_DIR_ABOVE else "↓"
    t = float(near.get("target", 0))
    d = distance_pct(price, t)
    if d is None:
        return "%s%.2f" % (op, t)
    return "%s%.2f %+.1f%%" % (op, t, d)


def ms_until_clear(session_date, now=None):
    """距 session_date 15:00:30 还有多少毫秒；已过或不可解析返回 0。"""
    dt = _clear_dt(session_date)
    if dt is None:
        return 0
    now = _as_beijing(now) or _beijing_now()
    return max(0, int((dt - now).total_seconds() * 1000))
