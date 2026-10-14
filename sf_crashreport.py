# -*- coding: utf-8 -*-
"""sf_crashreport —— 崩溃报告关键段抽取（v1.2.42）。

可观测性缺口修复：诊断 zip 原只列崩溃报告"文件名+时间"，不包含正文，导致
macOS 原生崩溃报告（.ips/.crash，如 OpenSSL 线程 SIGSEGV）无法定位根因。

本模块把"从崩溃报告抽取关键段"的逻辑提炼为纯函数（输入输出均为 str / list，
无 Qt / 无磁盘副作用），供单测覆盖；主窗 _export_diagnostics 仅做薄委托：

1. is_crash_report_name(fn)      —— 判断是否为本应用崩溃报告名（StockFloat 前缀 + .ips/.crash 后缀）
2. select_crash_reports(entries) —— 从 (文件名, mtime) 列表选出待打包报告：7 天内优先，
   最多 max_reports 个；7 天内不足则回退取最新 max_reports 个
3. extract_crash_key_sections(text, limit_bytes)
                                 —— 抽取关键段：异常类型/终止原因 / 崩溃线程+主线程堆栈 /
   二进制镜像关键行；输出体积 ≤ limit_bytes（默认每份 ≤ 200KB）；空文本返回 ""
   - 文本形如 .ips（JSON）→ 解析 exception/termination/threads/usedImages 渲染
   - 形如 .crash（纯文本）→ 关键行 + Thread N Crashed 堆栈块 + Binary Images 关键行
   解析失败或格式未知时安全兜底：返回空字符串或纯文本关键行，不抛异常。
"""

import json
import re

from sf_constants import APP_DISPLAY_NAME, LEGACY_EXECUTABLE_NAMES

# v1.4.55 程序改名，可执行文件名随之变成 Skyrocket；旧前缀继续认 ——
# 诊断包要能翻到改名前那半年的崩溃记录，否则「最近 7 天报告」会突然清空。
# v1.4.64（全方位复核）：名字从 sf_constants 派生，不再各存一份字面量；
# 同时删掉一处**全仓零引用**的旧单数常量 CRASH_NAME_PREFIX —— 留着它，
# 下次改名就会有人只改这一处而漏掉下面这个元组。
CRASH_NAME_PREFIXES = (APP_DISPLAY_NAME, *LEGACY_EXECUTABLE_NAMES)
CRASH_NAME_SUFFIXES = (".ips", ".crash")
DEFAULT_MAX_REPORTS = 2          # 建议最近 1~2 个
DEFAULT_WINDOW_DAYS = 7          # 7 天内优先
MAX_SEGMENT_BYTES = 200 * 1024   # 每份报告正文关键段 ≤ 200KB

# .crash 纯文本关键行（异常/终止原因/崩溃摘要）
_SUMMARY_RE = re.compile(
    r"(Exception Type:|Exception Codes:|Exception Note:|Termination Reason:|"
    r"Termination Signal:|Crashed Thread:|Triggered by Thread:|"
    r"Application Specific Information:|abort\(\) called|Last Exception Backtrace|SIGSEGV|SIGABRT|"
    r"SIGBUS|SIGILL|SIGFPE)")
_THREAD_HEADER_RE = re.compile(r"^Thread (\d+) Crashed")
_MAIN_THREAD_RE = re.compile(r"^Thread 0\b")
_BINARY_KEY_NAMES = ("StockFloat", "libcrypto", "libssl", "OpenSSL", "Python",
                     "QtCore", "QtGui", "QtWidgets", "PyQt6", "libSystem", "libffi")
_MAX_JSON_FRAMES = 60            # JSON 渲染每线程最多帧数
_MAX_JSON_IMAGES = 24            # usedImages 渲染最多镜像数


def is_crash_report_name(fn):
    """判断文件名是否为本应用崩溃报告（StockFloat 前缀 + .ips/.crash 后缀）。"""
    if not isinstance(fn, str):
        return False
    if not fn.startswith(CRASH_NAME_PREFIXES):
        return False
    return fn.endswith(CRASH_NAME_SUFFIXES)


def select_crash_reports(entries, now=None, max_reports=DEFAULT_MAX_REPORTS,
                         window_days=DEFAULT_WINDOW_DAYS):
    """从 (文件名, mtime) 列表选出待打包的崩溃报告。

    规则：7 天内产生的优先，最多 max_reports 个（按 mtime 新→旧）；
    7 天内没有任何报告时，回退取全量最新 max_reports 个。
    entries 为空返回 []；mtime 非法（非数值）条目忽略。
    返回按 mtime 新→旧排序的 [(fn, mtime), ...]。
    """
    import time as _time
    now = _time.time() if now is None else now
    if window_days is not None and window_days > 0:
        cutoff = now - window_days * 86400.0
    else:
        cutoff = None
    valid = []
    for item in entries:
        try:
            fn, mt = item[0], float(item[1])
        except (TypeError, ValueError, IndexError):
            continue
        if not is_crash_report_name(fn):
            continue
        valid.append((fn, mt))
    valid.sort(key=lambda e: e[1], reverse=True)
    if cutoff is None:
        return valid[:max_reports]
    fresh = [e for e in valid if e[1] >= cutoff]
    if fresh:
        return fresh[:max_reports]
    return valid[:max_reports]


# ---------------------------------------------------------------------------
# 关键段抽取
# ---------------------------------------------------------------------------

def extract_crash_key_sections(text, limit_bytes=MAX_SEGMENT_BYTES):
    """从崩溃报告文本抽取关键段，返回拼接文本（≤ limit_bytes，空输入/空结果返回 ""）。

    不抛异常：任何解析失败都走纯文本兜底或返回空串。
    """
    if not text:
        return ""
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    out = ""
    # 形如 .ips：正文以 JSON 对象开头
    # lstrip 带上 \ufeff：.ips 常带 UTF-8 BOM，不剥掉会让下面的 startswith("{") 判否
    stripped = text.lstrip("\ufeff \t\r\n")
    if stripped.startswith("{"):
        rendered = _render_json_sections(text)
        if rendered:
            out = rendered
    if not out:
        out = _render_plain_sections(text)
    return _clip(out, limit_bytes)


_TRUNCATED_MARK = "\n...[truncated]"


def _clip(text, limit_bytes):
    """按体积上限截断，附加截断标记。成品（含标记）≤ limit_bytes。

    v1.4.66 复核（2026-10-12）：旧实现先按 limit 切再追加标记，成品比上限多 15 字节，
    与本函数 docstring 及 extract_crash_key_sections 的"≤ limit_bytes"承诺不符。
    """
    limit_bytes = int(limit_bytes)
    if limit_bytes <= 0:
        return ""
    if len(text.encode("utf-8", "replace")) <= limit_bytes:
        return text
    body_limit = max(0, limit_bytes - len(_TRUNCATED_MARK.encode("utf-8")))
    cut = text.encode("utf-8", "replace")[:body_limit]
    # 避免在 UTF-8 多字节字符中间截断
    while cut:
        try:
            return cut.decode("utf-8") + _TRUNCATED_MARK
        except UnicodeDecodeError:
            cut = cut[:-1]
    return ""


def _render_json_sections(text):
    """对外入口：JSON 形崩溃报告渲染，**绝不向调用方抛异常**。

    v1.4.18 走查（#2）：extract_crash_key_sections 的契约是"任何解析失败都走
    纯文本兜底或返回空串"，但原 _render_json_sections 只把 json.loads 包在 try 里，
    渲染体（如 `int(base)` 撞上 base=null、images[i].get 撞上非 dict 元素）一旦抛错
    就会冒到导出调用方，被外层 try 吞掉 —— 结果是**整段 crash_reports（含其它正常
    报告）从诊断包里消失**。渲染失败应退回纯文本，而不是带走全部证据。
    """
    try:
        return _render_json_body(text)
    except Exception:
        return ""


def _split_ips_documents(text):
    """.ips 是**两份 JSON 文档拼在一起**：首行元数据、次行正文（本机 25 个真样本全是这个形状）。

    旧实现是 `json.loads(text[text.index("{"):])` —— 一次喂两份，必然
    `Extra data` 抛错 → 退回纯文本兜底 → 崩溃线程堆栈、终止原因、镜像清单
    **一条都进不了诊断包**（实测本机 14 个 .ips 里 0 个能解析，渲染长度只有
    186~196 字符的噪声）。也就是说 v1.4.18 建这条取证链以来，它一直是断的：
    今天分析 iMac 那枚崩溃靠的是用户手工发来的报告，不是我们的导出包。
    """
    dec = json.JSONDecoder()
    docs = []
    idx = text.find("{")
    while idx >= 0:
        while idx < len(text) and text[idx] in " \t\r\n":
            idx += 1
        if idx >= len(text):
            break
        try:
            obj, end = dec.raw_decode(text, idx)
        except ValueError:
            break
        docs.append(obj)
        idx = end
    return docs


def _pick_ips_body(docs):
    """返回 (元数据, 正文)。正文=带 threads/exception/bug_type 的那一份；取不到给 None。"""
    header = None
    body = None
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        if body is None and any(k in doc for k in ("threads", "exception",
                                                   "termination", "usedImages")):
            body = doc
        elif header is None:
            header = doc
    if body is None and len(docs) >= 2 and isinstance(docs[-1], dict):
        body = docs[-1]
        header = docs[0] if isinstance(docs[0], dict) else None
    return header, body


def _render_json_body(text):
    """.ips JSON 形崩溃报告：渲染异常/终止原因 + 崩溃线程与主线程堆栈 + 关键镜像。"""
    header, obj = _pick_ips_body(_split_ips_documents(text))
    if obj is None:
        return ""
    blocks = []

    # -- 身份行：进程/版本/系统/时刻（元数据那一半，旧实现整半都丢了）---------
    if isinstance(header, dict) and header:
        ident = []
        for key in ("app_name", "app_version", "bug_type", "os_version",
                    "product_name", "timestamp"):
            v = header.get(key)
            if v:
                ident.append("%s=%s" % (key, v))
        if ident:
            blocks.append("IPS header: %s" % " ".join(ident))

    # -- 异常类型 / 终止原因 -----------------------------------------------
    exc = obj.get("exception")
    if isinstance(exc, dict):
        parts = []
        for k in ("type", "signal", "codes"):
            if exc.get(k):
                parts.append("%s=%s" % (k, exc[k]))
        if parts:
            blocks.append("Exception: %s" % " ".join(parts))
    elif isinstance(exc, str) and exc:
        blocks.append("Exception: %s" % exc)

    term = obj.get("termination")
    if isinstance(term, dict):
        parts = []
        for k in ("indicator", "namespace", "codes", "details", "reasons"):
            v = term.get(k)
            if isinstance(v, list):
                v = " | ".join(str(x) for x in v)
            if v:
                parts.append("%s=%s" % (k, v))
        if parts:
            blocks.append("Termination: %s" % " ".join(parts))
    # legacyInfo 兜底（Thread 0 Crashed 摘要）
    legacy = obj.get("legacyInfo")
    if isinstance(legacy, dict):
        for k in ("exception", "termination"):
            v = legacy.get(k)
            if v:
                blocks.append("legacyInfo.%s: %s" % (k, v))

    # -- 崩溃线程 / 主线程堆栈 ----------------------------------------------
    threads = obj.get("threads")
    if isinstance(threads, list):
        images = obj.get("usedImages")
        images = images if isinstance(images, list) else []
        crashed_idx = None
        # 崩溃线程字段名随 macOS 版本不同（C = Crashed），两种写法都试一遍
        triggered = obj.get("triggeredByCThread", obj.get("triggeredByThread"))
        if isinstance(triggered, int):
            crashed_idx = triggered
        if crashed_idx is None:
            for i, t in enumerate(threads):
                if isinstance(t, dict) and (t.get("crashed") or t.get("triggered")):
                    crashed_idx = i
                    break
        if crashed_idx is None:
            crashed_idx = 0   # 报告没标出崩溃线程时退回线程 0（主线程），至少给出一段堆栈
        if crashed_idx < len(threads):
            blocks.append("--- Crashed Thread (index %d) ---" % crashed_idx)
            blocks.append(_frames_text(threads[crashed_idx], images))
        # 崩溃的若就是主线程，这里再输出一遍只会重复同一段堆栈
        if len(threads) > 1 and not (crashed_idx == 0):
            blocks.append("--- Main Thread (index 0) ---")
            blocks.append(_frames_text(threads[0], images))

    # -- asi（Apple System Information：崩溃那一刻系统给的原因线索）----------
    asi = obj.get("asi")
    if isinstance(asi, dict) and asi:
        blocks.append("--- asi ---")
        for key in list(asi)[:6]:
            blocks.append("%s: %s" % (key, str(asi.get(key))[:200]))

    # -- 二进制镜像关键行 ----------------------------------------------------
    images = obj.get("usedImages")
    if isinstance(images, list) and images:
        blocks.append("--- Key Images ---")
        picked = [i for i in images if isinstance(i, dict) and
                  any(kn in str(i.get("name", "")) for kn in _BINARY_KEY_NAMES)]
        if not picked:
            picked = images[:_MAX_JSON_IMAGES]
        for im in picked[: _MAX_JSON_IMAGES]:
            if not isinstance(im, dict):
                continue
            name = im.get("name", "?")
            base = im.get("base", 0)
            arch = im.get("arch", "")
            try:
                # base 可能是 null / 字符串 / 超大值：坏条目跳过而非让整段渲染抛错。
                base_int = int(base)
            except (TypeError, ValueError):
                continue
            blocks.append("0x%x %s %s" % (base_int, arch, name))
    return "\n".join(blocks)


def _frames_text(thread, images):
    """把单个 thread 的 frames 渲染为多行文本。"""
    frames = thread.get("frames") if isinstance(thread, dict) else None
    if not isinstance(frames, list):
        return "(no frames)"
    out = []
    for fr in frames[:_MAX_JSON_FRAMES]:
        if not isinstance(fr, dict):
            continue
        sym = fr.get("symbol")
        if not sym:
            img = fr.get("imageIndex")
            if (isinstance(img, int) and isinstance(images, list)
                    and 0 <= img < len(images) and isinstance(images[img], dict)):
                sym = str(images[img].get("name", "?")).split("/")[-1]
            else:
                sym = "?"
        out.append("  %s" % sym)
    if len(frames) > _MAX_JSON_FRAMES:
        out.append("  ...(%d more frames)" % (len(frames) - _MAX_JSON_FRAMES))
    return "\n".join(out)


def _render_plain_sections(text):
    """.crash 纯文本：摘要行 + 崩溃/主线程堆栈块 + 二进制镜像关键行。"""
    lines = text.splitlines()
    if not lines:
        return ""
    blocks = []

    # 1) 异常 / 终止 / 崩溃摘要行
    summary = []
    for ln in lines:
        if _SUMMARY_RE.search(ln):
            summary.append(ln.strip())
    if summary:
        blocks.append("--- Summary ---")
        blocks.extend(summary[:60])

    # 2) 崩溃线程 + 主线程堆栈块
    stack = _plain_stack_blocks(lines)
    if stack:
        blocks.append(stack)

    # 3) 二进制镜像关键行
    images = _plain_key_images(lines)
    if images:
        blocks.append("--- Key Images ---")
        blocks.extend(images)

    return "\n".join(blocks)


def _plain_stack_blocks(lines):
    """抽取 .crash 的 Thread N Crashed 与 Thread 0（主线程）堆栈块。"""
    blocks = []
    buffer = []
    on = None     # 'crashed' / 'main'（当前正在捕获的线程块）

    def _flush():
        if on is not None and buffer:
            blocks.append("--- %s ---" % ("Thread N Crashed" if on == "crashed" else "Main Thread"))
            blocks.append("\n".join(buffer).rstrip())

    for ln in lines:
        if ln.strip().startswith("Binary Images:"):
            _flush()
            break
        if _THREAD_HEADER_RE.match(ln):
            _flush()
            buffer = [ln.strip()]
            on = "crashed"
            continue
        if _MAIN_THREAD_RE.match(ln):
            _flush()
            buffer = [ln.strip()]
            on = "main"
            continue
        if re.match(r"^Thread \d", ln):
            # 新线程头结束上一块（非崩溃/非主线程，仅结束）
            _flush()
            buffer = []
            on = None
            continue
        if on is not None:
            buffer.append(ln)
    _flush()
    return "\n".join(blocks)


def _plain_key_images(lines):
    """.crash Binary Images 段：优先含已知关键库名的行，否则取段首若干行。"""
    capture = False
    in_section = []
    for ln in lines:
        if re.match(r"^Binary Images:", ln.strip()):
            capture = True
            continue
        if capture:
            if ln.strip() == "":
                break
            in_section.append(ln)
    if not in_section:
        return []
    keyed = [ln for ln in in_section if any(kn in ln for kn in _BINARY_KEY_NAMES)]
    # 整段都没有已知关键库时退回段首若干行，保证至少能看到加载了什么
    picked = keyed if keyed else in_section[:8]
    return picked[:60]
