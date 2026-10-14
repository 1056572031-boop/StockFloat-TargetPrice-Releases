# -*- coding: utf-8 -*-
"""模块化拆分 v1.2.13：sf_network —— 由 stock_float_window.py 搬移生成，行为零变化。"""

import collections
import http.client
import ssl
import threading
import time
import urllib.request, urllib.parse
import uuid
from sf_constants import CONN_POOL_IDLE_TIMEOUT, CONN_POOL_MAX, HTTP_RETRIES, HTTP_TIMEOUT, MONITOR_RING_CAPACITY, MONITOR_WINDOW_SECONDS
from sf_logging import feature_enabled, log_line
from sf_http import ResponsePolicyError, check_https, read_limited, safe_urlopen

_LAST_HTTP_STATUS = None      # 兼容既有诊断/测试的进程级快照（不可用于业务判定）
_STATUS_LOCAL = threading.local()   # 业务判定读**线程本地**值：并发请求互不覆盖（见 getter）


def _set_last_http_status(status):
    """记录本线程本次请求状态，避免并发请求互相覆盖业务判定。"""
    global _LAST_HTTP_STATUS
    _LAST_HTTP_STATUS = status
    _STATUS_LOCAL.value = status


def get_last_http_status():
    """返回**当前线程**最近一次请求的响应状态码。

    行情、分时与日历可并发请求；业务分支读取线程本地值，杜绝别的请求覆盖本次
    4xx/超时状态。进程级快照仅保留给原有诊断兼容使用。
    """
    # 直接写入旧变量的诊断工具/历史测试仍应可读；业务请求一律经
    # _set_last_http_status 写线程本地值，因此并发时不会串扰。
    return getattr(_STATUS_LOCAL, "value", _LAST_HTTP_STATUS)

# v1.2.95 第2轮走查：只有"重试有可能成功"的状态码才值得重试。
# 403/404/410 这类明确拒绝，重试三次结果完全一样，却让每次刷新多等约 1 秒 ——
# 交易时段行情轮询是 10Hz 量级，等于把一次故障的体感拖长十倍。
_RETRYABLE_STATUS = frozenset({408, 425, 429})   # 请求超时 / 过早 / 限流

def _retryable(status):
    """该 HTTP 状态码是否值得重试：408/425/429 与全部 5xx 重试，其余 4xx 不重试。"""
    try:
        code = int(status)
    except (TypeError, ValueError):
        # 拿不到状态码（连接被重置等本地异常）时无法判定，按"值得重试"处理：
        # 这类失败多是瞬时抖动，重试的代价小于直接放弃本次行情。
        return True
    return code in _RETRYABLE_STATUS or code >= 500

# v1.2.95 第2轮走查：连接池回退 urllib 的告警在断网/接口挂掉时可达 10 条/秒，
# 会把真正有用的故障日志挤掉（单文件 5MB × 3 代轮转）。首条必记，其后每 N 条记一次。
_FALLBACK_LOG_EVERY = 30
_fallback_log_count = 0

def _log_fallback(msg):
    """连接池回退 urllib 的限流告警：首条必记，其后每 _FALLBACK_LOG_EVERY 条记一条并附累计值。

    断网/接口挂掉时这条路径可达 10 条/秒，不限流会把真正有用的故障日志挤出
    单文件 5MB × 3 代的轮转窗口 —— "累计 N 次"就是为事后补看留的账。
    """
    global _fallback_log_count
    _fallback_log_count += 1
    if _fallback_log_count == 1 or _fallback_log_count % _FALLBACK_LOG_EVERY == 0:
        log_line("%s（同类已累计 %d 次）" % (msg, _fallback_log_count), "WARNING")

class QuoteDataError(Exception):
    """行情接口返回数据格式/数值异常（区别于网络故障，不计入网络失败）"""

class ConnectionPool:
    """6.4 v1.2.5：按 host 维护 HTTPSConnection 连接池，复用已建立的 TCP/TLS 连接，
    消除每次请求的 TLS 握手开销。线程安全（threading.Lock）；空闲连接超时自动关闭。
    连接被服务端主动断开时，借用方在请求异常路径通过 discard() 销毁并重建。"""

    def __init__(self, max_connections=CONN_POOL_MAX, idle_timeout=CONN_POOL_IDLE_TIMEOUT):
        """池结构：host -> [(conn, last_used)]，self._lock 保护全部读写。

        上限与空闲回收时长都来自 sf_constants；空闲超时由 get_connection 借用时检查，
        这里不做后台回收线程。
        """
        self.max_connections = max_connections
        self.idle_timeout = idle_timeout
        self._pool = {}          # host -> list of (conn, last_used)
        self._lock = threading.Lock()

    def _create(self, host, timeout):
        """新建一条 TLS 连接；系统 SSL context 创建失败时降级裸连（同 urllib 兜底），不抛。"""
        try:
            ctx = ssl.create_default_context()
            return http.client.HTTPSConnection(host, timeout=timeout, context=ctx)
        except Exception:
            # 默认 SSL context 创建失败时降级（同 urllib 兜底行为），不阻断请求
            return http.client.HTTPSConnection(host, timeout=timeout)

    def get_connection(self, host, timeout=HTTP_TIMEOUT):
        """取出一条可用连接；池中无可用则新建。空闲超时/已关闭的连接自动丢弃。"""
        now = time.monotonic()
        with self._lock:
            pool = self._pool.setdefault(host, [])
            # 后进先出：最后归还的连接最可能还活着。从队首取会先撞上最早归还、
            # 最接近空闲超时的那批，白做一轮 close + 重建。
            while pool:
                conn, used = pool.pop()
                if now - used > self.idle_timeout:
                    try:
                        conn.close()
                    except Exception:
                        pass
                    continue
                if getattr(conn, "sock", None) is None:
                    try:
                        conn.close()
                    except Exception:
                        pass
                    continue
                conn.timeout = timeout
                set_timeout = getattr(conn.sock, "settimeout", None)
                if callable(set_timeout):
                    set_timeout(timeout)
                return conn
        return self._create(host, timeout)

    def release_connection(self, host, conn):
        """请求成功后将连接放回池中复用；超上限则关闭释放。"""
        with self._lock:
            pool = self._pool.setdefault(host, [])
            if len(pool) < self.max_connections:
                pool.append((conn, time.monotonic()))
                return
        try:
            conn.close()
        except Exception:
            pass

    def discard(self, host, conn):
        """连接已损坏/服务端关闭：销毁而非放回池，下次请求自动重建。"""
        try:
            conn.close()
        except Exception:
            pass

    def close_all(self):
        """关闭复用连接，并唤醒正在重试等待的网络请求。"""
        with _NET_ABORT_LOCK:
            _NET_ABORT.set()
        with self._lock:
            for pool in self._pool.values():
                for conn, _ in pool:
                    try:
                        conn.close()
                    except Exception:
                        pass
            self._pool.clear()

# 日志/监控路径的 URL 脱敏名单（sanitize_url_for_log 消费）：query 里命中这些键名的
# 参数**整对丢弃**（不是打码保留键名），只留非敏感参数；名单按键名小写匹配、不猜值形态。
# 除 query 外还剥 netloc 里的 user:pass@ 与 fragment，解析异常一律退化为不带 query 的
# host+path —— 绝不回吐原始 URL。这份名单是对外可审计项（公开快照整仓带走），
# 新增键时先想清楚为什么。
_SENSITIVE_QUERY_KEYS = frozenset({
    "token", "key", "apikey", "api_key", "access_token", "refresh_token",
    "sign", "secret", "password", "passwd", "auth", "ticket", "cookie",
    "session", "sessionid", "jwt", "code2session",
})

def sanitize_url_for_log(url):
    """v1.2.12：从完整 URL 中剥离 token 等敏感 query 参数，仅保留
    scheme+host+path 与必要的非敏感参数，供监控埋点/日志使用。
    同时剥离 userinfo（如 user:pass@host）与 fragment，避免凭据泄露。
    任何解析异常都退化为不带 query 的 host+path，绝不回吐原始 URL。"""
    try:
        parts = urllib.parse.urlsplit(url)
        netloc = parts.netloc
        if "@" in netloc:
            netloc = netloc.rsplit("@", 1)[1]  # 去掉 user:pass@ 前缀
        kept = [(k, v) for k, v in urllib.parse.parse_qsl(
                    parts.query, keep_blank_values=True)
                if k.lower() not in _SENSITIVE_QUERY_KEYS]
        query = urllib.parse.urlencode(kept) if kept else ""
        return urllib.parse.urlunsplit(
            (parts.scheme, netloc, parts.path, query, ""))
    except Exception:
        try:
            scheme, rest = url.split("://", 1)
            return scheme + "://" + rest.split("?", 1)[0]
        except Exception:
            return url

class NetMonitor:
    """5.1 v1.2.5：网络层监控埋点模块。无侵入式拦截 http_get 请求，统计
    请求成功率、平均响应延迟、错误码分布；内存环形缓冲暂存最近记录，
    滑动窗口周期聚合写入日志。所有逻辑 try/except 包裹，绝不抛出影响主流程。"""

    def __init__(self, capacity=MONITOR_RING_CAPACITY, window=MONITOR_WINDOW_SECONDS):
        """环形缓冲容量与聚合窗口都来自 sf_constants；is_debug 开着才逐请求打 [Monitor] 行。"""
        self._ring = collections.deque(maxlen=capacity)
        self._window = window
        self._lock = threading.Lock()
        self.is_debug = False
        self._last_flush = 0.0

    def begin_request(self, url):
        """请求发出前记录上下文；失败返回 None（埋点不可用不影响请求）。
        v1.2.12：api_path 经 sanitize_url_for_log 脱敏，剥离 token 等敏感参数，
        环形缓冲/debug 打印/聚合日志均不落完整 URL。"""
        try:
            return {"request_id": uuid.uuid4().hex,
                    "api_path": sanitize_url_for_log(url),
                    "start": time.monotonic()}
        except Exception:
            return None

    def end_request(self, ctx, is_success, status_code, response_size, error_msg):
        """响应返回后记录结果并写入环形缓冲；跨过窗口边界时输出聚合指标。"""
        try:
            if ctx is None:
                return
            ctx["end"] = time.monotonic()
            ctx["latency_ms"] = (ctx["end"] - ctx["start"]) * 1000.0
            ctx["status_code"] = status_code
            ctx["is_success"] = bool(is_success)
            ctx["response_size"] = response_size
            ctx["error_msg"] = error_msg
            with self._lock:
                self._ring.append(ctx)
                if self.is_debug:
                    print("[Monitor] %s | code=%s | %.1fms | ok=%s" % (
                        ctx["api_path"], status_code, ctx["latency_ms"], is_success))
            self._maybe_flush()
        except Exception:
            pass

    def _maybe_flush(self):
        """每 window 秒输出一次聚合指标到日志（成功率/平均延迟/错误码 Top N）。"""
        try:
            now = time.monotonic()
            with self._lock:
                if now - self._last_flush < self._window:
                    return
                self._last_flush = now
                recs = [r for r in self._ring if now - r["start"] <= self._window]
                if not recs:
                    return
                total = len(recs)
                ok = sum(1 for r in recs if r["is_success"])
                avg = sum(r.get("latency_ms", 0.0) for r in recs) / total
                err = {}
                for r in recs:
                    if not r["is_success"]:
                        c = r.get("status_code", -1)
                        err[c] = err.get(c, 0) + 1
                top = sorted(err.items(), key=lambda kv: -kv[1])[:3]
                top_s = ";".join("code=%s x%d" % (k, v) for k, v in top) or "none"
                log_line("monitor: win=%ds total=%d ok=%d rate=%.1f%% avg=%.1fms err[%s]"
                         % (self._window, total, ok, ok * 100.0 / total, avg, top_s))
        except Exception:
            pass

    def snapshot(self):
        """供诊断导出：返回环形缓冲内统计快照（dict），失败返回 None"""
        try:
            with self._lock:
                total = len(self._ring)
                if not total:
                    return {"total": 0, "ok": 0, "rate": None, "avg_latency_ms": None}
                ok = sum(1 for r in self._ring if r["is_success"])
                avg = sum(r.get("latency_ms", 0.0) for r in self._ring) / total
                return {"total": total, "ok": ok,
                        "rate": round(ok * 100.0 / total, 1),
                        "avg_latency_ms": round(avg, 1)}
        except Exception:
            return None

_NET_POOL = ConnectionPool()     # 6.4 v1.2.5：全局连接池（quit 时 close_all 释放）

_NET_MONITOR = NetMonitor()      # 5.1 v1.2.5：全局网络监控

# v1.2.95 第9轮走查：退出时让阻塞在"重试间休眠"的请求能立刻醒来走中止分支，
# 而非傻等 0.5s×重试次数（弱网/退出时单请求最坏多耗数秒纯等待）。_NET_ABORT 由
# close_all()（_shutdown_threads 在 aboutToQuit 路径调用）置位；仅用于跳过休眠，
# 在途请求仍受各自 socket timeout 约束（不强行打断），符合"新增等待须 ≤500ms 有界"。
_NET_ABORT = threading.Event()
_NET_ABORT_LOCK = threading.Lock()   # 只串"置位/换新令牌"两处微秒级临界区，不护网络本身


def _begin_network_round():
    """为一轮新请求取得独立中止令牌；关闭后的后续请求可安全恢复。"""
    global _NET_ABORT
    with _NET_ABORT_LOCK:
        if _NET_ABORT.is_set():
            # 不清除旧对象：旧请求持有它，仍能观察到 close_all 的中止信号。
            _NET_ABORT = threading.Event()
        else:
            _NET_ABORT.clear()
        return _NET_ABORT


def _interruptible_sleep(secs, abort_event=None):
    """替代 time.sleep：close_all 置位本轮令牌时立即返回。"""
    (abort_event or _NET_ABORT).wait(secs)


def _http_get_pooled(url, gbk=True, timeout=HTTP_TIMEOUT, retries=HTTP_RETRIES, headers=None,
                     _abort_event=None, _deadline=None):
    """6.4 v1.2.5：基于连接池的 HTTP GET（复用已建立 TCP/TLS 连接）。
    连接被服务端断开时自动销毁并重试重建；整体失败抛最后一次异常。"""
    _abort_event = _abort_event or _begin_network_round()
    check_https(url)
    deadline = _deadline or time.monotonic() + timeout * (retries + 2) + 0.5 * retries
    u = urllib.parse.urlparse(url)
    host = u.netloc
    path = u.path or "/"
    if u.query:
        path = path + "?" + u.query
    hdrs = dict(headers or {"User-Agent": "Mozilla/5.0"})
    global _LAST_HTTP_STATUS
    last = None
    for attempt in range(retries + 1):
        conn = None
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or _abort_event.is_set():
                raise TimeoutError("HTTP request cancelled or deadline exceeded")
            conn = _NET_POOL.get_connection(host, min(timeout, remaining))
            conn.request("GET", path, headers=hdrs)
            resp = conn.getresponse()
            body = read_limited(resp, deadline)
            status = resp.status
            if status >= 400:
                # 4xx/5xx：响应已读完，连接不可安全复用，销毁后按重试处理
                _set_last_http_status(status)   # 本请求失败状态，供封禁特征检测/埋点
                _NET_POOL.discard(host, conn)
                conn = None
                last = OSError("HTTP %d" % status)
                if not _retryable(status):
                    break          # 明确拒绝：重试无意义，立即失败
                if attempt < retries:
                    _interruptible_sleep(0.5, _abort_event)
                continue
            _NET_POOL.release_connection(host, conn)
            conn = None
            _set_last_http_status(status)
            return body.decode("gbk" if gbk else "utf-8", errors="replace")
        except Exception as exc:
            last = exc
            if conn is not None:
                _NET_POOL.discard(host, conn)
                conn = None
            if isinstance(exc, ResponsePolicyError):
                raise
            if attempt < retries:
                _interruptible_sleep(0.5, _abort_event)
    raise last

def http_get(url, gbk=True, timeout=HTTP_TIMEOUT, retries=HTTP_RETRIES, headers=None,
             pooled=True):
    """HTTP GET 统一入口（5.1/6.4 v1.2.5）：优先走连接池复用并埋点；
    连接池路径任何异常自动回退原 urllib 实现，埋点失败静默跳过，不影响主流程。

    v1.3.65：新增 pooled 参数。连接池 _NET_POOL 无锁，并发取/还连接会竞态，
    故**与行情线程并发**的请求（搜索的东财兜底）必须 pooled=False 走 urllib
    独立连接，不碰共享连接池。"""
    global _LAST_HTTP_STATUS
    # close_all() 只中止当时在途请求的重试等待；明确发起的新一轮请求换用新令牌。
    abort_event = _begin_network_round()
    check_https(url)
    deadline = time.monotonic() + timeout * (retries + 2) + 0.5 * retries
    mon = None
    # 一次请求只允许落一条埋点：兜底路径走到末尾即失败，但若前面已记过成功，
    # 不能再追加一条失败，否则成功率会把同一次请求算两遍。
    mon_finished = False
    if feature_enabled("net_monitor"):
        mon = _NET_MONITOR.begin_request(url)
    try:
        # v1.2.95 第2轮走查：进入本次请求先清空状态码。此前它只在"拿到响应"时
        # 被赋值，连接失败/超时/被拒时保留上一次的值 —— 于是断网时导出的诊断
        # 信息会显示 last_http_status=200，等于谎报"接口一切正常"，误导排查。
        _set_last_http_status(None)
        # v1.3.65：pooled=False 时跳过连接池，直接走 urllib 独立连接（并发安全）
        pool_attempted = feature_enabled("conn_pool") and pooled
        if pool_attempted:
            try:
                body = _http_get_pooled(url, gbk, timeout, retries, headers, abort_event, deadline)
                if mon is not None:
                    # v1.4.18 走查（#6）：埋点归因必须读本次请求所在线程的线程本地状态。
                    # _LAST_HTTP_STATUS 是进程级快照（见其定义注释"不可用于业务判定"），
                    # 并发日历/行情请求会把它覆盖成别的服务端码，污染封禁特征检测。
                    _NET_MONITOR.end_request(mon, True, get_last_http_status(), len(body), None)
                    mon_finished = True
                return body
            except Exception as exc:
                if isinstance(exc, ResponsePolicyError):
                    raise
                _log_fallback("conn_pool fallback to urllib: %r" % (exc,))
        # v1.2.69：若刚从连接池失败转来，已是第 2 重试轮；此处不再全量重试，
        # 封顶单请求总耗时（否则最坏约 26s 且长期持锁阻塞行情刷新）。
        # 连接池本身被关闭时 pool_attempted 为 False，urllib 才是主路径，保留完整重试。
        fb_retries = 0 if pool_attempted else retries
        req = urllib.request.Request(url, headers=headers or {"User-Agent": "Mozilla/5.0"})
        last = None
        for attempt in range(fb_retries + 1):
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or abort_event.is_set():
                    raise TimeoutError("HTTP request cancelled or deadline exceeded")
                with safe_urlopen(req, timeout=min(timeout, remaining)) as r:
                    _set_last_http_status(r.status)
                    body = read_limited(r, deadline).decode("gbk" if gbk else "utf-8", errors="replace")
                    if mon is not None:
                        _NET_MONITOR.end_request(mon, True, get_last_http_status(), len(body), None)
                    return body
            except urllib.request.HTTPError as he:
                # v1.2.28：4xx/5xx 也记录响应状态码，供封禁特征检测与埋点使用
                _set_last_http_status(he.code)
                last = he
                # v1.2.95：两处都应为 fb_retries —— 兜底路径 fb_retries=0 时本应
                # 一次就返回，却因误用 retries 白睡 0.5s；403 之类也不再重试。
                # v1.2.95 走查补漏：上面只免掉了"重试前的睡眠"，却没跳出循环 ——
                # 连接池被关闭（pool_attempted=False）时 fb_retries 仍是全局 retries，
                # 一个 403 会在同一次 http_get 里照发 3 次请求，正与注释的承诺相反。
                # 不可重试的状态码直接 break，只发这一次。
                if not _retryable(he.code):
                    break
                if attempt < fb_retries:
                    _interruptible_sleep(0.5, abort_event)
            except Exception as exc:
                if isinstance(exc, ResponsePolicyError):
                    raise
                last = exc
                if attempt < fb_retries:
                    _interruptible_sleep(0.5, abort_event)
        if mon is not None and not mon_finished:
            _NET_MONITOR.end_request(mon, False, -1, 0, str(last))
            mon_finished = True
        raise last
    except Exception as exc:
        if mon is not None and not mon_finished:
            _NET_MONITOR.end_request(mon, False, -1, 0, str(exc))
            mon_finished = True
        raise
