# -*- coding: utf-8 -*-
"""诊断包自动采集层（2026-10-13 新增，负责人提的"别再每几版手动对齐诊断包"）。

这一层解决的是**搬运**，不是**立意**
--------------------------------
`outputs/诊断导出逐版核查-v1.4.27至v1.4.66-2026-10-12.md` 那 40 版对账的根子在于：
产品每加一处运行态（计数器、末态、失败原因、间隔），都要有人**记得**把它接进
`_export_diagnostics`。没人记得就没人知道 —— 39 个实发版本里 29 个引入了当时远程
读不出来的状态，而全量回归两年全绿。靠人记得的事迟早不发生（AGENTS §3.1 那条
「build_release.sh 从未调用过回归」同根），所以搬运这一半改成机器做：

导出时遍历①已加载的产品模块的模块级变量，②传进来的对象（主窗，以及它持有的
我们自己的对象）的实例字典，凡**名字命中状态词表**且**值可 JSON 化**的一律收进
`state.json`。新增一个 `self._xxx_fail_count`、一处模块级 `_DELIVER_COUNTS`，
不需要碰导出函数就自动进包 —— 这是本层唯一承诺的"自动"。

答不上来的那半交给闸门：`tools/diag_coverage_check.py` 按"本轮改过的产品模块必须
被包覆，否则逐条申报理由"硬拦发版。因为**"这项坏了用户会说什么"是语义判断**，
机器给不出；机器只能保证它一定被看见过。两条口径合起来才是负责人要的那句话。

三条硬边界（都不许放宽）
----------------------
1. **只读数据，一个函数都不调**。这个遍历发生在用户机器上、发生在导出那一下：
   调 `stats()` / `snapshot()` 之类 getter 等于把"取诊断"变成"跑业务"，任何一处
   副作用（写盘、起线程、访问网络）都算导出器的缺陷。可取值只来自模块级变量与
   实例字典。想被自动收到，就把状态写成一个变量 —— 这与 AGENTS §5.7 那条
   "改了哪个模块的运行态就要在包里回答"是同一个方向。
2. **外发的包里不许出现家目录与账号名**（AGENTS §5.7 第二条的同一族）。字符串值
   一律过 `_scrub`：家目录 → `~`，`$USER` / `getpass` 取到的登录名 → `<user>`；
   名字里带 token/secret/password/credential/api_key/phone/mail 的键**整个不收**，
   只在 `skipped_secret_named` 里留一个数量 —— 数量本身是线索，值不是。
3. **体积与深度有上限**。`MAX_OBJECTS` / `MAX_ITEMS` / `MAX_STR_LEN` / `MAX_DEPTH`
   四道钳，超限就记进 `truncated`（哪个容器被截、截到多少），不静默丢。静默截断
   等于把"这条状态很长"伪装成"这条状态不见了"，与台账 G11/G24 那一族同形。

判据：`tests/test_diag_autocollect.py`。里面第一条就是"新造一个模块、写一个新
计数器、什么都不接，导出里必须有它" —— 那才是这层的验收，不是字段存在性检查。
"""

from __future__ import annotations

import getpass
import math
import os
import re
import sys
from itertools import islice
from typing import Any, Iterable, NamedTuple
from sf_privacy import redact_urls_in_text

PROD_PREFIX_EXEMPT = ("sf_constants",)   # 见 _module_names：那份是设计值的唯一来源，不进自动层


class Needle(NamedTuple):
    """一条脱敏规则：`pattern` 命中就换成 `marker`；`is_regex` 决定走正则还是字面量。"""

    pattern: str
    marker: str
    is_regex: bool
    hint: str = ""

MAX_OBJECTS = 240      # 最多展开多少个对象（主窗 + 它持有的自有对象，一层层爬）
MAX_ITEMS = 60         # 单个 dict/list 最多收多少项
MAX_STR_LEN = 220      # 单个字符串截多长
MAX_DEPTH = 3          # 容器嵌套最深几层
MAX_ISSUES = 40        # 取证行最多留几条（再多就是刷日志，不叫取证）

# 状态词表：名字按 `_` 切段后，任一段命中即算"这是一条运行态"。
# 为什么按段而不是子串：`second` 含 `sec`、`mode` 含在 `models` 里，子串匹配会把
# 一堆布局与业务名字一起捞进来，噪音一大就没人读这个文件了。
STATE_TOKENS = frozenset({
    "count", "counts", "counter", "fail", "fails", "failure", "failures",
    "error", "errors", "err", "errs", "issues", "streak", "retry", "retries",
    "tries", "attempts", "left", "age", "last", "first", "since", "state",
    "mode", "stage", "phase", "reason", "gap", "gaps", "lag", "prune",
    "residue", "total", "totals", "sum", "hit", "hits", "miss", "misses",
    "frames", "ms", "sec", "px", "pending", "active", "alive", "running",
    "stopped", "paused", "denied", "granted", "auth", "allowed", "blocked",
    "enabled", "disabled", "level", "ok", "bad", "changed", "dropped",
    "shrink", "hidden", "visible", "pref", "prefs", "switch", "fallback",
    "degraded", "stale", "fresh", "expired", "timeout", "wait", "waiting",
    "opened", "closed", "held", "backoff", "interval", "budget", "quota",
    "reject", "rejects", "swallow", "swallowed", "neutralized", "renamed",
})

# 整键拒收（值不外发，只留数量）：这些名字一旦出现，收进来就是隐私事故。
SECRET_TOKENS = frozenset({
    "token", "secret", "password", "passwd", "credential", "credentials",
    "api_key", "apikey", "phone", "mobile", "mail", "email", "wechat", "wxid",
    "cookie", "cookies", "authcode",
    "sign", "signature", "jwt", "ticket",
})

_URL_CREDENTIAL_RE = re.compile(
    r"([A-Za-z][A-Za-z0-9+.-]*://)[^\s/@:]+(?::[^\s/@]*)?@"
)
_SECRET_VALUE_RE = re.compile(
    r"([\"']?(?:token|api[_-]?key|secret|sign(?:ature)?|password|passwd|credential|cookie|auth(?:code)?|session|jwt|ticket)"
    r"[\"']?\s*[:=]\s*)(?:\"(?:\\.|[^\"\\\r\n])*\"?|'(?:\\.|[^'\\\r\n])*'?|[^\s,;&\"']+)",
    re.IGNORECASE,
)
_QUERY_SECRET_RE = re.compile(
    r"([?&](?:token|api[_-]?key|key|secret|sign|password|cookie|auth|ticket|session|jwt|code2session)"
    r"[^=&]*=)[^&\s]*", re.IGNORECASE,
)
_SECRET_HINTS = ("token", "key", "secret", "sign", "password", "passwd", "credential",
                 "cookie", "auth", "session", "jwt", "ticket")

# 只收"数据"，不收"资源"：Qt 对象、线程、锁、模块、函数、类实例都跳过。
# 为什么不在这里 isinstance 判 Qt 基类：见下面 _skip_value 的说明（为取诊断而
# import GUI 框架是倒因为果），所以判据用类型名形状。
PROD_PACKAGE_PREFIX = ("sf_",)


def _looks_prod(module_name: str) -> bool:
    """判断一个 `__module__` 是不是本程序自己的代码（用于决定要不要爬进它的实例）。"""
    if not module_name:
        return False
    head = module_name.split(".")[0]
    if head == "stock_float_window" or head == "renderer":
        return True
    return head.startswith(PROD_PACKAGE_PREFIX)


def _segments(name: str) -> list[str]:
    split = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name.replace("-", "_"))
    return [part.lower() for part in split.split("_") if part]


def is_state_name(name: str) -> bool:
    """名字命中状态词表即算"这是一条运行态"。

    全大写**不**当排除条件（第一版排了，实测立刻漏掉 `sf_notify._DELIVER_COUNTS`
    这类"名字写成常量形状、其实是模块级可变计数器"—— 那正是本轮最想自动收到的东西）。
    真正的过滤器是词表本身：`BOTTOM_ROW_ASCENT` 一个词都不命中，`_DELIVER_COUNTS`
    命中 counts，`POLL_INTERVAL_NORMAL` 命中 interval（生效间隔是多少本身就是排障信息）。
    """
    return any(seg in STATE_TOKENS for seg in _segments(name))


def is_secret_name(name: str) -> bool:
    segs = _segments(name)
    return (any(seg in SECRET_TOKENS for seg in segs)
            or "api_key" in "_".join(segs))


def identity_needles(issues: list[str]) -> tuple[Needle, ...]:
    """需要被替换掉的身份串与它的标记。

    四条实测形状，少一条就会漏身份：

    1. `expanduser("~")` —— 装机版里这就是家目录，最常出现在 path 类字段；
    2. `/Users/<登录名>` 与 `/home/<登录名>` —— **HOME 被改时这两条才顶用**。判据
       `tests/test_diag_autocollect.py::test_home_directory_and_account_name_are_scrubbed`
       就是靠夹具把 `HOME` 指到临时目录跑出来的：那时 `expanduser` 给的是临时目录，
       而产品里存着的绝对路径（上一次运行写进 config 的）仍然是 /Users/<账号>/...。
       第一版只有第 1 条，结果那句被换成 `/Users/<user>/Library/...` —— 账号名没了，
       但这是谁的机器还看得见形状，且 `~` 标记也没了。
    3. 裸登录名 —— 出现在"用户 X 的目录打不开"这类文本里。这条**必须走词边界**：
       账号名常常是常见英文子串（`ann` 撞 cancelled、`jack` 撞 jackpot），字面替换
       会把正文里同样的片段吃掉，读包的人看到的是一堆被啃过的字。
    4. 路径类先换、名字类后换，避免部分重叠时短的先把长的切碎。

    取不到就少一条并**在 issues 里留痕**（AGENTS §6 第 9 条：兜异常不许静默 —— 这一层
    静默就等于"这条身份没被替换"在包里看不见）。
    """
    out: list[Needle] = []
    users: list[str] = []
    for label, getter in (("USER", lambda: os.environ.get("USER")),
                          ("USERNAME", lambda: os.environ.get("USERNAME"))):
        try:
            value = getter()
            if value and len(value) >= 2:
                users.append(str(value))
        except Exception as exc:
            _note(issues, "identity %s: %s" % (label, exc))
    try:
        name = getpass.getuser()
        if name and len(name) >= 2:
            users.append(name)
    except Exception as exc:
        _note(issues, "getpass.getuser: %s" % (exc,))
    users = list(dict.fromkeys(users))
    try:
        home = os.path.expanduser("~")
        if home and home != "~":
            out.append(Needle(re.escape(home), "~", True, home))
    except Exception as exc:
        _note(issues, "expanduser: %s" % (exc,))
    for user in users:
        for base in ("/Users/", "/home/"):
            out.append(Needle(re.escape(base + user), "~", True, base + user))
        # 前后不许粘着词字符或路径分隔符，否则那就不是替换身份，而是啃正文
        out.append(Needle(r"(?<![\w.\-/])%s(?![\w-])" % re.escape(user), "<user>", True, user))
    return tuple(out)


def scrub_text(text: str, needles: tuple[Needle, ...]) -> str:
    """把身份串换成标记。路径类换成 `~`（目录结构要留着可判代次），账号名换成 `<user>`。"""
    result = redact_urls_in_text(text)
    result = re.sub(r"-----BEGIN " + r"(?:[A-Z]+ )?PRIVATE KEY-----.*?-----END "
                    + r"(?:[A-Z]+ )?PRIVATE KEY-----", "<REDACTED>", result, flags=re.DOTALL)
    result = re.sub(r"\A.*?-----END " + r"(?:[A-Z]+ )?PRIVATE KEY-----", "<REDACTED>", result, flags=re.DOTALL)
    result = re.sub(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b", "<REDACTED>", result)
    for needle in needles:
        if needle.hint and needle.hint not in result:
            continue
        if needle.is_regex:
            result = re.sub(needle.pattern, needle.marker, result)
        else:
            result = result.replace(needle.pattern, needle.marker)
    # 状态值经常是异常文本，而异常文本可能把凭据写成 `token=...` 或 URL
    # basic-auth。键名过滤挡不住这种形状；这里保留排障正文，只抹掉值。
    if "/Users/" in result or "/home/" in result:
        result = re.sub(r"((?:/Users/|/home/))([^/\s\"'\\]+)", r"\1<USER>", result)
    if "\\Users\\" in result:
        result = re.sub(r"([A-Za-z]:\\\\?Users\\\\?)([^\\\\\s\"']+)", r"\1<USER>", result)
    if "://" in result:
        result = _URL_CREDENTIAL_RE.sub(r"\1", result)
    lower = result.lower()
    if not any(hint in lower for hint in _SECRET_HINTS):
        return result
    if "authorization" in lower:
        result = re.sub(
            r"[\"']?\bauthorization\b[\"']?\s*[:=]\s*"
            r"(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|digest[^\r\n]*|(?:bearer\s+|basic\s+)?[^\s,;]+)",
            "Authorization=<REDACTED>", result, flags=re.IGNORECASE,
        )
    if "?" in result or "&" in result:
        result = _QUERY_SECRET_RE.sub(lambda m: m.group(1) + "<REDACTED>", result)
    return _SECRET_VALUE_RE.sub(lambda m: m.group(1) + "<REDACTED>", result)


def _drop_count_marker(out: dict[str, Any], dropped: int) -> None:
    """"名字命中词表、状态却装在一个对象里"的条数必须留痕，不许当不存在。

    自动层刻意**不调任何取值函数**（见模块开头第 1 条边界），所以这类状态收不上来。
    留一个数量，是让读包的人知道"这里还有一层没进去"，而不是以为这个容器是空的。
    """
    if dropped:
        out["_state_inside_objects"] = dropped


def _note(issues: list[str], text: str) -> None:
    """留痕上限由调用方管，这里只管追加（超限时 collect 会裁并注明还剩多少条）。"""
    issues.append(text)


def is_data(value: Any) -> bool:
    """值是不是"数据"：标量、字符串、字节串、容器。类/函数/模块/实例都不算。

    为什么单独一个判据：`vars(模块)` 里混着**再导来的符号**（`from x import EditorState`
    之后 `EditorState` 也在模块字典里）。第一版没区分，实测每个模块都冒出一条
    `_dropped_non_serializable: 1` —— 那是"这里有个类"被写成"这里有条状态没收上来"，
    读包的人会当真去找那条状态。
    """
    return type(value) in (bool, int, float, str, bytes, bytearray, list, tuple,
                           set, frozenset, dict) or value is None


def is_state_holder(value: Any) -> bool:
    """值是一个"我们自己造的实例" —— 里面很可能有状态，但自动层不替人决定。"""
    if is_data(value) or callable(value) or isinstance(value, type):
        return False
    return _looks_prod(getattr(type(value), "__module__", "") or "")


def encode(value: Any, needles: tuple[Needle, ...], depth: int,
           truncated: list[str], path: str) -> tuple[bool, Any]:
    """把一条值转成 JSON 可序列化形状。返回 (收下了, 值)；(False, None) = 这一项不收。

    超限**必须留痕**（`truncated` 里写清是哪条路径、原样多大），否则"这条状态被
    截断"与"这条状态本来就这样"在包里同形。
    """
    if not is_data(value):
        return False, None
    if isinstance(value, bool):
        return True, value
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            # NaN/Infinity 是 Python 的 json 扩展，不是 JSON：写出去以后浏览器与
            # 严格解析器直接报错，等于这一份现场对外不可用。产品里确有 nan 的来源
            # （sf_ui_render.py:1663/1689 的坐标净化），所以这条不是假想敌。
            truncated.append("%s: %r 不是有限数，写成 null" % (path, value))
            return True, None
        return True, value
    if isinstance(value, (str, bytes, bytearray)):
        try:
            text = value.decode("utf-8", "replace") if isinstance(value, (bytes, bytearray)) else value
        except Exception:
            return False, None
        text = scrub_text(text, needles)
        if len(text) > MAX_STR_LEN:
            truncated.append("%s: 截断 %d→%d 字符" % (path, len(text), MAX_STR_LEN))
            text = text[:MAX_STR_LEN] + "…"
        return True, text
    if value is None:
        return True, None
    if depth >= MAX_DEPTH:
        truncated.append("%s: 深度超过 %d 层未展开" % (path, MAX_DEPTH))
        return True, None
    if isinstance(value, dict):
        if len(value) > MAX_ITEMS:
            truncated.append("%s: dict 共 %d 键，只取前 %d" % (path, len(value), MAX_ITEMS))
        out: dict[str, Any] = {}
        secrets = 0
        dropped = 0
        for key, item in islice(value.items(), MAX_ITEMS):
            if type(key) not in (str, int, float, bool, type(None)):
                dropped += 1
                continue
            raw = str(key)
            if is_secret_name(raw):
                secrets += 1
                continue
            # 键也要脱敏：字典常常以路径为键（`{"/Users/<账号>/Library/...": 3}`），
            # 值那半边换了、键没换，等于把"这是谁的机器"留在包里。判据
            # tests/test_diag_autocollect.py::test_dict_keys_are_scrubbed_too
            name = scrub_text(raw, needles)
            base = name
            slot = 2
            while name in out:
                name = "%s#%d" % (base, slot)
                slot += 1
            ok, encoded = encode(item, needles, depth + 1, truncated, "%s.%s" % (path, name))
            if ok:
                out[name] = encoded
            else:
                dropped += 1
        if secrets:
            out["_secret_keys_dropped"] = secrets
        _drop_count_marker(out, dropped)
        return True, out
    if isinstance(value, (list, tuple, set, frozenset)):
        if len(value) > MAX_ITEMS:
            truncated.append("%s: 序列共 %d 项，只取前 %d" % (path, len(value), MAX_ITEMS))
        out_list: list[Any] = []
        dropped = 0
        for index, item in enumerate(islice(value, MAX_ITEMS)):
            ok, encoded = encode(item, needles, depth + 1, truncated, "%s[%d]" % (path, index))
            if ok:
                out_list.append(encoded)
            else:
                dropped += 1
        if dropped:
            truncated.append("%s: %d 项是对象而非数据，未收" % (path, dropped))
        return True, out_list
    # 其余类型（实例、函数、模块、锁、Qt 桥接件）一律不当作数据收。
    return False, None


DESIGN_VALUE_MODULES = ("sf_constants",)
"""整份排除的模块：那份是**设计值**的唯一来源（颜色/尺寸/阈值/URL），不是运行态。

为什么不收（复核 F8 实测）：一次采集里 `sf_constants` 独自贡献 58 条，全是
`FACE_GUARD_NEAR_FACE_W`、`*_INTERVAL`、`*_PX` 这类"必然等于源码里的值"的常量 ——
装机版按版本号就能反查，占着包却没有"这一台机器"的信息量。真正需要被远程确认的
阈值已经由人工接线那几份说了（face_guard 分组、runtime.minute.interval_sec 等）。
排除动作**必须写进包里**（`meta.excluded_modules`），不然读的人以为这个模块没有值。
"""


def _module_names() -> list[str]:
    """当前进程里已加载的产品模块名（只取真的 import 过的，绝不为了取诊断去 import）。

    采集器自己排除在外：`STATE_TOKENS`/`MAX_ITEMS` 这类词表收进包等于让包去解释
    它自己，读的人只会多困惑一份、少一份真信号。
    """
    out: list[str] = []
    for name, module in list(sys.modules.items()):
        if "." in name or name == __name__:
            continue
        if name in DESIGN_VALUE_MODULES:
            continue
        if not _looks_prod(name):
            continue
        if getattr(module, "__name__", "") == name:
            out.append(name)
    return sorted(out)


def collect_module(module: Any, needles: tuple[Needle, ...],
                   issues: list[str], truncated: list[str]) -> dict[str, Any]:
    """一个模块的模块级运行态（不碰 `__` 双下划线、不调任何取值函数）。"""
    try:
        vars_map = dict(vars(module))
    except Exception as exc:                                  # 极少数 C 扩展模块没有 __dict__
        _note(issues, "vars(%s): %s" % (getattr(module, "__name__", "?"), exc))
        return {}
    return _collect_values(vars_map, module.__name__, needles, issues, truncated)


def _collect_values(vars_map: dict[str, Any], label: str, needles: tuple[Needle, ...],
                    issues: list[str], truncated: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    dropped = 0
    secrets = 0
    for name, value in vars_map.items():
        if name.startswith("__") or not is_state_name(name):
            continue
        if is_secret_name(name):
            secrets += 1
            continue
        if not is_data(value):
            if is_state_holder(value):
                dropped += 1        # 状态在对象里，自动层不展开，只留一条指针
            continue
        try:
            ok, encoded = encode(value, needles, 0, truncated, "%s.%s" % (label, name))
        except Exception as exc:
            _note(issues, "%s.%s: %s" % (label, name, exc))
            continue
        if ok:
            out[name] = encoded
        else:
            dropped += 1
    _drop_count_marker(out, dropped)
    if secrets:
        out["_secret_keys_dropped"] = secrets
    return out


def _child_objects(owner: Any) -> Iterable[Any]:
    """从宿主对象里挑"我们自己的实例"，作为下一层去爬（浅一层，带数量钳）。"""
    try:
        members = list(vars(owner).items())
    except Exception:
        return ()
    out: list[Any] = []
    for name, value in members:
        if is_secret_name(name):
            continue
        if isinstance(value, (bool, int, float, str, bytes, list, tuple, dict, set)):
            continue
        if value is None or callable(value):
            continue
        if _looks_prod(getattr(type(value), "__module__", "") or ""):
            out.append(value)
    return out[:MAX_OBJECTS]


def collect(owner: Any = None, extra_objects: Iterable[Any] = ()) -> dict[str, Any]:
    """自动采集总入口：`{meta, modules, objects, issues, truncated}`。

    `owner` 传主窗实例；`extra_objects` 供判据与调用方补宿主（例如测试里塞一个
    新造的替身对象）。取不到的部分一律进 `issues` 点名，函数本身**不抛错**——
    它一旦被导出流程用 try 兜住又静默返回空，就等于"这台机器没有状态"，那正是
    台账 G11/G24 那一族说假话的形状。
    """
    issues: list[str] = []
    truncated: list[str] = []
    needles = identity_needles(issues)
    payload: dict[str, Any] = {
        "meta": {
            "origin": "naming-convention",
            "why": "新增状态位按词表自动进包，不需要改导出函数；语义类现场仍由人工接线",
            "tokens": len(STATE_TOKENS),
            "python": sys.version.split()[0],
            "loaded_product_modules": 0,
            # 排除动作写进包里：读的人看得见"这一份是刻意没扫的"，而不是"这个模块没值"
            "excluded_modules": list(DESIGN_VALUE_MODULES),
            "scrub": ("home", "/Users/<user>", "/home/<user>", "<user>(词边界)"),
        },
        "modules": {},
        "objects": {},
        "issues": issues,
        "truncated": truncated,
    }
    for name in _module_names():
        module = sys.modules.get(name)
        if module is None:
            continue
        try:
            found = collect_module(module, needles, issues, truncated)
        except Exception as exc:
            _note(issues, "module %s: %s" % (name, exc))
            continue
        payload["modules"][name] = found
    payload["meta"]["loaded_product_modules"] = len(payload["modules"])

    queue: list[tuple[str, Any, int]] = []
    if owner is not None:
        queue.append((type(owner).__name__, owner, 0))
    for item in extra_objects:
        queue.append(("extra:" + type(item).__name__, item, 0))
    seen_ids: set[int] = {id(owner)} if owner is not None else set()
    depth_guard = 0
    while queue and len(payload["objects"]) < MAX_OBJECTS and depth_guard < MAX_OBJECTS * 4:
        depth_guard += 1
        label, node, depth = queue.pop(0)
        try:
            vars_map = dict(vars(node))
        except Exception as exc:
            _note(issues, "vars(%s): %s" % (label, exc))
            continue
        obj_found = _collect_values(vars_map, label, needles, issues, truncated)
        unique_label = label
        slot = 2
        while unique_label in payload["objects"]:
            unique_label = "%s#%d" % (label, slot)
            slot += 1
        payload["objects"][unique_label] = obj_found
        if depth < 1:
            for child in _child_objects(node):
                if id(child) in seen_ids:
                    continue
                seen_ids.add(id(child))
                queue.append((type(child).__name__, child, depth + 1))
    if queue:
        truncated.append("objects: 超过 %d 个对象上限，其余对象未展开" % MAX_OBJECTS)
    # 留痕本身也要有上限，但它必须**说清自己截了多少**：把 truncated 悄悄裁短，
    # 读包的人会以为"只有这几条被截"，那是 §5.7 那条"取不到值必须点名"的反面。
    if len(truncated) > MAX_ISSUES:
        overflow = len(truncated) - MAX_ISSUES
        del truncated[MAX_ISSUES:]
        truncated.append("（另有 %d 条截断留痕未展开）" % overflow)
    if len(issues) > MAX_ISSUES:
        overflow = len(issues) - MAX_ISSUES
        del issues[MAX_ISSUES:]
        issues.append("（另有 %d 条取值失败未点名）" % overflow)
    # 读取失败的异常文字来自第三方/系统调用，不能因为它走的是 issues/truncated
    # 旁路就绕过与状态值相同的隐私规则。
    for lines in (issues, truncated):
        for index, line in enumerate(lines):
            lines[index] = scrub_text(str(line), needles)
    return payload
