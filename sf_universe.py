# -*- coding: utf-8 -*-
"""v1.3.66：本地股票字典 —— 让「输代码 / 输名字」的搜索告别网络往返。

为什么需要它
------------
实测（2026-09-14，深圳电信）：腾讯 smartbox 主搜索中位 48ms、东财北交所兜底
中位 89ms，两者并行后总耗时中位仍有 56ms，且偶发 218~255ms 的尖峰（TLS 握手
与抖动）。而全 A 股票不过 5900 只，名字一个月也未必变一次 —— 把这张表缓存到
本地后，本地匹配实测 0.2~1.4ms，比联网快 30~200 倍，且断网也能搜。

接管范围（关键：不能为了快把搜索结果改掉）
------------------------------------------
本地只接管「确定无疑」的搜索，命中即返回、0 网络往返：
  1. 代码精确（600519 / sh600519）
  2. 名称精确（贵州茅台）
  3. 名称包含且**唯一**（"茅台" -> 贵州茅台，"宁德" -> 宁德时代）
多结果的模糊词（"贵州" 3 条、"银行" 38 条）**一律不接管** —— 这类搜索的含金量
在相关性排序（实测腾讯给"贵州"首条=贵州茅台，本地按代码序会给贵州轮胎），
那是搜索引擎的活儿，本地做不好，继续走联网。

兜底：联网返回 0 条时，再用本地模糊匹配补一次（离线/接口抽风时仍可用）。

安全边界
--------
- 只有缓存文件、没有别的落盘；路径独立（securities.json），不碰 config.json、
  last_quote.json，更不碰自选股。
- 任何一步出错（解析失败、网络失败、写盘失败）都静默退回联网搜索，绝不抛给调用方。
- 测试环境（unittest 且非冻结产物）默认停用，避免打桩用例被真实字典污染。
"""

import json
import os
import queue
import re
import sys
import threading
import time
import unicodedata
from functools import lru_cache
from itertools import islice
from operator import index

from sf_constants import (UNIVERSE_MAX_PAGES, UNIVERSE_MIN_INTERVAL,
                          UNIVERSE_MIN_ROWS, UNIVERSE_PAGE_GAP,
                          UNIVERSE_PAGE_RETRIES, UNIVERSE_PAGE_SIZE,
                          UNIVERSE_PATH, UNIVERSE_TIMEOUT, UNIVERSE_TTL,
                          UNIVERSE_WORKERS)

# 测试环境一律停用（与 sf_constants 的落盘隔离同款兜底，但这里还要防"真实字典
# 污染打桩结果"：用例给 http_get 打桩后调 search_stocks，若字典先命中，返回的
# 就不是桩数据了）。SF_UNIVERSE_DISABLE=0 可显式强制启用。
_DISABLED = (os.environ.get("SF_UNIVERSE_DISABLE") == "1"
             or ("unittest" in sys.modules and not getattr(sys, "frozen", False)
                 and os.environ.get("SF_UNIVERSE_DISABLE") != "0"))

# ---------- 东财全市场列表（含沪深 + 北交所） ----------
# 排序必须用代码 fid=f12：按涨跌幅 fid=f3 排序时行情实时变动会让翻页漂移，
# 实测 60 页只捞回 3013/5913 条（重复 + 遗漏）。
_FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
# 主域名短时高频会被断连（RemoteDisconnected），此时降级到延时行情域名 ——
# 我们要的只是代码/名称这张静态表，延时不延时无所谓。
_HOSTS = ("push2.eastmoney.com", "push2delay.eastmoney.com")
_URL_TPL = ("https://%s/api/qt/clist/get?pn=%d&pz=%d&po=0&np=1"
            "&fltt=2&invt=2&fid=f12&fs=" + _FS + "&fields=f12,f13,f14")


def _page_url(host, pn, pz=UNIVERSE_PAGE_SIZE):
    """拼单页 URL：host（主/延时域名轮换）+ 页号 pn + 页大小 pz（默认 UNIVERSE_PAGE_SIZE）。"""
    return _URL_TPL % (host, pn, pz)

_lock = threading.RLock()
_rows = []            # [(full_code, name), ...]  例如 ("sh600519", "贵州茅台")
_by_code = {}         # full_code -> name
_by_name = {}         # name -> full_code（重名时保留先出现的）
_loaded = False       # 是否已尝试过从磁盘加载
_refresh_started = False
_last_attempt = 0.0
_last_error = None


def _market_of(code):
    """6 位代码 -> 市场前缀。北交所必须写在沪市前面：920xxx 是新代码段，
    按"9 开头=沪市"判会把贝特瑞(920185)错判成 sh（v1.3.66 探针实测踩到）。"""
    if code.startswith(("43", "83", "87", "88", "920")):
        return "bj"
    if code.startswith(("60", "68", "900")):
        return "sh"
    if code.startswith(("000", "001", "002", "003", "300", "301", "200")):
        return "sz"
    return ""


def _norm_name(s):
    """名称归一化：去掉所有空白。

    东财部分名称带空格（实测返回的是 "五 粮 液"），不去空格的话用户搜"五粮液"
    精确匹配和包含匹配会双双落空，白白退回联网。
    """
    return "".join((s or "").split())


def fold_name(s):
    """搜索比较用的折叠：全角转半角（NFKC）、去所有空白、英文字母统一小写。

    只用于"比较"，不用于显示：字典里存的仍是东财给的原名（"TCL科技"、"ST晨鸣"、
    "京东方Ａ"）。搜索词和字典名称必须走**同一个**折叠，否则只要名称里有英文字母，
    本地匹配就永远对不上（实测 5922 只里有 389 只含半角字母，317 只是 ST）。
    全角数字和全角字母是中文输入法的常见产物，NFKC 一并收成半角。
    """
    return "".join(unicodedata.normalize("NFKC", str(s or "")).split()).casefold()


@lru_cache(maxsize=16384)
def _name_key(name):
    """字典名称的折叠键（带缓存：一次搜索要对 5900 行各折叠一次）。

    匹配函数一律经由它取键而不直接调 fold_name，测试可以在这一处换成计数字符串。
    """
    return fold_name(name)


_CODE_SHAPE = re.compile(r"(?:(sh|sz|bj)\.?)?([0-9]{6})(?:\.(sh|sz|bj))?")


def code_shape(text):
    """判断输入是不是"完整的 6 位代码"，返回 (前缀, 数字)，前缀可为空串；不是则返回 None。

    先走 fold_name，所以全角数字、首尾与中间的空白、大小写都已收成标准形态。
    只认 ASCII 数字：NFKC 之后仍不是 ASCII 的数字（阿拉伯-印度数字等）不算代码，
    原样交给名称搜索，不会被当成代码送去取行情。
    交易所可以写在前面（sh600519、sh.600519）或后面（600519.SH，券商软件复制出来的
    常见写法）；前后都写且不一致时不当作代码。
    """
    m = _CODE_SHAPE.fullmatch(fold_name(text))
    if not m:
        return None
    head, digits, tail = m.groups()
    if head and tail and head != tail:
        return None
    return (head or tail or "", digits)


def _index(rows):
    """用一份全量结果整体替换三份内存索引（不可增量合并：翻页漂移会带来重复/遗漏）。

    _rows 存规范化后的 (full_code, name)；_by_code / _by_name 用 setdefault 建索引，
    重名代码/重名名称都保留先出现的一条 —— 字典只要"能唯一指到一条"就够用。
    """
    global _rows, _by_code, _by_name
    _rows = [(c, _norm_name(n)) for c, n in rows]
    _by_code = {}
    _by_name = {}
    for full, name in _rows:
        _by_code.setdefault(full, name)
        _by_name.setdefault(name, full)


def load(force=False):
    """从磁盘加载字典。失败（文件不存在/损坏/结构不符）返回 False 并静默。"""
    global _loaded
    if _DISABLED:
        return False
    with _lock:
        if _loaded and not force:
            return bool(_rows)
        _loaded = True
        try:
            with open(UNIVERSE_PATH, "r", encoding="utf-8") as f:
                obj = json.load(f)
        except Exception:
            return False
        raw = (obj or {}).get("rows") if isinstance(obj, dict) else obj
        if not isinstance(raw, list) or len(raw) < 1000:
            # 少于 1000 条视为残缺（正常约 5900 条），宁可退回联网也不用半张表
            return False
        rows = []
        for item in raw:
            try:
                full, name = item[0], item[1]
            except Exception:
                continue
            if isinstance(full, str) and isinstance(name, str) and full and name:
                rows.append((full, name))
        if len(rows) < 1000:
            return False
        _index(rows)
        return True


def _mtime():
    """落盘字典的修改时间；读不到（无文件/无权限）按 0.0 —— is_stale 据此判过期。"""
    try:
        return os.path.getmtime(UNIVERSE_PATH)
    except Exception:
        return 0.0


def is_stale():
    """字典是否缺失或过期。"""
    if _DISABLED:
        return True
    if not _rows:
        return True
    return (time.time() - _mtime()) > UNIVERSE_TTL


def _fetch_page(host, pn, pz=UNIVERSE_PAGE_SIZE):
    """拉一页并解析出 diff 行列表（每行含 f12 代码 / f13 市场 / f14 名称）。

    retries=0 是刻意的：重试预算在调用方 _refresh_once 的页级循环里
    （UNIVERSE_PAGE_RETRIES + 页间隔 UNIVERSE_PAGE_GAP），在这层再叠一次就成了双倍重试。
    """
    from sf_network import http_get          # 延迟导入，避免与 sf_network 形成导入环
    data = http_get(_page_url(host, pn, pz),
                    gbk=False, timeout=UNIVERSE_TIMEOUT, retries=0)
    obj = json.loads(data)
    return ((obj or {}).get("data") or {}).get("diff") or []


def _pick_host():
    """选一个当前可用的域名。先探 1 页（pz=1，几乎不耗流量）。"""
    for host in _HOSTS:
        try:
            if _fetch_page(host, 1, 1):
                return host
        except Exception:
            continue
    return None


def _refresh_once():
    """拉全量并落盘。任何异常都吞掉，由调用方决定下一次重试。"""
    global _last_error
    host = _pick_host()
    if not host:
        _last_error = "no usable host"
        return False
    pages = {}
    failed_pages = set()
    q = queue.Queue()
    for p in range(1, UNIVERSE_MAX_PAGES + 1):
        q.put(p)

    def worker():
        while True:
            try:
                pn = q.get_nowait()
            except Exception:
                return
            for attempt in range(UNIVERSE_PAGE_RETRIES + 1):
                try:
                    pages[pn] = _fetch_page(host, pn)
                    break
                except Exception as exc:      # 单页失败不影响其它页
                    globals()["_last_error"] = repr(exc)
                    if attempt == UNIVERSE_PAGE_RETRIES:
                        failed_pages.add(pn)
                        pages[pn] = []
                    else:
                        time.sleep(0.4 * (attempt + 1))
            # 页间必须留间隔：并发压上去会被东财断连（实测并发 3 成功率即崩到 30%）
            if UNIVERSE_PAGE_GAP:
                time.sleep(UNIVERSE_PAGE_GAP)

    ths = [threading.Thread(target=worker, name="sf-universe-%d" % i, daemon=True)
           for i in range(UNIVERSE_WORKERS)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()

    if failed_pages:
        _last_error = "incomplete pages: %s" % ",".join(map(str, sorted(failed_pages)))
        return False

    rows = []
    seen = set()
    for diff in pages.values():
        for item in diff or []:
            try:
                code = (item.get("f12") or "").strip()
                name = _norm_name(item.get("f14"))
            except Exception:
                continue
            mkt = _market_of(code)
            if not mkt or not name:
                continue
            full = mkt + code
            if full in seen:
                continue
            seen.add(full)
            rows.append((full, name))
    if len(rows) < UNIVERSE_MIN_ROWS:
        # 缺页不写盘：宁可用旧字典，也不能把一张残缺的表当作完整表用
        _last_error = "too few rows: %d" % len(rows)
        return False
    # 原子写：先写临时文件再 replace，避免写一半崩掉留下坏文件。
    # 临时名必须每次唯一：两个程序实例同时刷新时，固定的 ".tmp" 会互相
    # 截断或删掉对方正在写的文件，导致其中一次刷新失败。
    tmp = "%s.%d.%d.tmp" % (UNIVERSE_PATH, os.getpid(), time.time_ns())
    try:
        d = os.path.dirname(UNIVERSE_PATH)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), "count": len(rows),
                       "rows": [[c, n] for c, n in rows]}, f, ensure_ascii=False)
        os.replace(tmp, UNIVERSE_PATH)
    except Exception as exc:
        _last_error = "write failed: %r" % (exc,)
        try:
            os.unlink(tmp)
        except Exception:
            pass
        return False
    with _lock:
        _index(rows)
    return True


def refresh_async():
    """后台刷新（幂等：同一时刻只跑一个；受最小间隔保护）。"""
    global _refresh_started, _last_attempt
    if _DISABLED:
        return False
    with _lock:
        now = time.time()
        if _refresh_started:
            return False
        if (now - _last_attempt) < UNIVERSE_MIN_INTERVAL:
            return False
        _refresh_started = True
        _last_attempt = now

    def _run():
        global _refresh_started
        try:
            _refresh_once()
        except Exception:
            pass
        finally:
            with _lock:
                _refresh_started = False

    threading.Thread(target=_run, name="sf-universe-refresh", daemon=True).start()
    return True


def ensure_ready():
    """每次搜索前调用：加载字典 + 必要时后台刷新。绝不阻塞、绝不抛异常。"""
    if _DISABLED:
        return
    try:
        if not _loaded:
            load()
        if is_stale():
            refresh_async()
    except Exception:
        pass


_DIGITS_SHAPE = re.compile(r"(sh|sz|bj)?([0-9]+)")


def _split_digits(kw):
    """关键词 -> (显式前缀或空串, 数字段)；不是"可选前缀 + 纯数字"则返回 ("", "")。"""
    m = _DIGITS_SHAPE.fullmatch(fold_name(kw))
    return (m.group(1) or "", m.group(2)) if m else ("", "")


def _digits_of(kw):
    """从关键词剥出纯数字代码：认 sh/sz/bj 前缀（剥掉取后段），也认裸数字；否则空串。"""
    return _split_digits(kw)[1]


def prefixed_digits(text):
    """输入若是「可选交易所前缀 + 任意位数字」，返回 (前缀或空串, 数字)，否则 None。

    和 code_shape 的区别：不要求恰好 6 位。编辑窗用它识别 `sh6005` 这类写了前缀、
    还没敲完或前缀写错的代码输入，按数字模糊搜索，而不是当成名称去搜。
    """
    prefix, digits = _split_digits(text)
    return (prefix, digits) if digits else None


def match_digits(digits, limit=50):
    """按代码数字（不看交易所前缀）列出字典里的股票；联网拿不到候选时的离线兜底。

    字典只收股票，指数、ETF 不在其中，所以这里列不全；它的作用是断网时前缀写错的
    输入至少还能给出同号股票，而不是什么都没有。
    """
    if _DISABLED or not digits:
        return []
    with _lock:
        return [(c, n) for c, n in _rows if c[2:].startswith(digits)][:limit]


def match_confident(kw):
    """高置信匹配：只有"确定无疑"才返回结果，否则返回 []（交给联网）。

    1) 代码精确   2) 名称精确   3) 名称包含且唯一

    两条口径（2026-10-29 走查补）：
    - **显式交易所前缀必须被尊重**。用户写了 sh000001（上证指数）就只查 sh000001，
      查不到就是查不到；旧写法只取后面的数字、把三个交易所挨个试，结果 sh000001 悄悄
      变成深市的平安银行，窗口还立刻关了。裸 6 位数字没有前缀可尊重，仍三个交易所依次试。
    - 名称比较一律经 `_name_key` 折叠（全角转半角、小写、去空白），返回的仍是原名。
    """
    if _DISABLED or not kw:
        return []
    k = fold_name(kw)
    if not k:
        return []
    with _lock:
        if not _rows:
            return []
        prefix, digits = _split_digits(k)
        if digits:
            for pre in ((prefix,) if prefix else ("sh", "sz", "bj")):
                full = pre + digits
                if full in _by_code:
                    return [(full, _by_code[full])]
        # 名称精确：先扫一遍只比相等。必须单独成一遍 —— 歧义要在"第二个包含命中"
        # 就提前判定，若同一遍里边找精确边判歧义，"平安"这种本身就是全名、又被
        # "平安银行""平安保险"包含的词会被误判成歧义。
        for code, name in _rows:
            if _name_key(name) == k:
                return [(code, name)]
        hit = None
        for code, name in _rows:
            if k in _name_key(name):
                if hit is not None:
                    return []
                hit = (code, name)
        if hit is not None:
            return [hit]
    return []


def match_fuzzy(kw, limit=50):
    """兜底模糊匹配：名称前缀优先，其次任意位置包含。用于联网 0 结果时救场。"""
    if _DISABLED or not kw:
        return []
    k = fold_name(kw)
    if not k:
        return []
    with _lock:
        if not _rows:
            return []
        count = None if limit is None else index(limit)
        # Negative slicing remains supported for compatibility with existing callers.
        if count is not None and count < 0:
            prefix = [(c, n) for c, n in _rows if _name_key(n).startswith(k)]
            return prefix[:count] if prefix else [(c, n) for c, n in _rows if k in _name_key(n)][:count]
        if count == 0:
            return []
        prefix = list(islice(((c, n) for c, n in _rows if _name_key(n).startswith(k)), count))
        if prefix:
            return prefix
        return list(islice(((c, n) for c, n in _rows if k in _name_key(n)), count))


def index_ready():
    """本地字典是否已装载可用条目。

    v1.4.18 建议#5 曾让编辑器据此判断"能不能拿本地字典校验一个裸 6 位码是否存在"。
    编辑器自 v1.4.50 起不再调用它（存在性只由行情探针回答，字典只负责"确认存在"），
    现在只有诊断导出读它，用来回答"字典是否已装载"。"""
    with _lock:
        return bool(_rows)


def covers_segment(code):
    """本地字典**有没有资格**替这个代码段说话。

    为什么需要它（v1.4.50 走查，实测坐实的回归）：字典按东财 `fs` 段拉取，本机实测
    5917 行只含股票段（2 位前缀恰好是 60/68/00/30/92），基金/ETF（51、15、58）、
    可转债（11）、国债（01）、B 股（90、20）**一段都没有**。而 `sf_editor` 在
    v1.4.18 用它做过存在性校验 —— 于是"字典里查不到"被当成了"代码不存在"，
    输 510300/159915/588000 一律提示「未找到该代码」，**ETF 彻底加不进悬浮窗**。
    而 `sf_quote.fetch_quote("sh510300")` 实测正常返回「沪深300ETF华泰柏瑞 4.326」，
    即：能报价、不能录入。v1.4.18 之前裸 6 位码是直通录入的，所以这是那次修复
    引入的回归，不是旧限制。

    判据从字典自身推导，不写死段表：只要字典里存在同 2 位前缀的条目，字典就是
    该段的权威（查不到=真不存在，可以拦）；一段都没有时字典无权发言，
    调用方必须改用别的存在性证据（行情探测），既不能盲放也不能误拒。

    `index_ready()` 为假时一律 False —— 空字典对任何段都没有发言权。

    2026-10-29 注：产品代码里已没有调用方（编辑器不再用"字典覆盖不覆盖这一段"来决定
    拦不拦，见 `sf_editor.verify_bare_code`），只剩 `tests/test_v1450_etf_entry.py`
    对它本身的判据。保留是因为删除属于"死代码清理"，要走独立批次的治理流程。
    """
    digits = _digits_of(code or "")
    if len(digits) != 6:
        return False
    head = digits[:2]
    with _lock:
        return bool(_rows) and any(full[2:4] == head for full in _by_code)


# 编辑器拒绝录入的计数（诊断导出用）：1.4.51 修过"510300 加不进去"，但修完之后
# 远程仍然区分不了"字典没刷新 / 该段不由字典发言 / 行情探测失败"三件事 ——
# 编辑器拒绝时只改一行 hint 文字，无日志无计数。拒绝那一侧按原因就地落账。
_REJECTS: dict = {}


def note_reject(reason):
    """编辑器拒绝录入/搜索无匹配时记一笔，进 sf_universe.stats() 的 rejects。

    reason 取编辑器调用点给的短标签（现有 "probe_says_missing" /
    "probe_empty_or_wrong_code" / "search_no_match" / "prefix_mismatch" /
    "prefix_no_match" / "invalid_code_shape"），这里不做二次解释 —— 口径归调用方。
    只有两行 dict 操作、reason 全部来自调用点字面量，不设兜异常：这里一旦写坏，
    宁可在调用点的外层守卫里留痕（编辑器三处调用各有自己的 try/log_swallow），
    也不在这里悄悄吞。
    """
    _REJECTS[reason] = _REJECTS.get(reason, 0) + 1


def stats():
    """内存索引的自省快照（测试与诊断用）：开关状态、条数、是否过期、落盘路径、最近一次错误。

    只读内存与文件元数据，不发网络请求、不触发刷新 —— 想知道字典为什么没建好，
    看 last_error；is_stale 为 True 且 disabled 为 False 时查 _mtime 与 UNIVERSE_TTL。
    """
    return {
        "disabled": _DISABLED,
        "count": len(_rows),
        "stale": is_stale() if not _DISABLED else True,
        "path": UNIVERSE_PATH,
        "last_error": _last_error,
        "rejects": dict(_REJECTS),
    }
