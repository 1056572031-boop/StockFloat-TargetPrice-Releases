#!/usr/bin/env python3
"""逃逸台账：只算两个数字 —— 线上逃逸数、平均定位时长。

为什么要这么个东西（2026-10-09 定）：
    负责人明确不再用覆盖率当进度指标，改盯两个数：**线上逃逸数**与**平均定位时长**
    是不是在持续下降。摸底结论（同日凌晨，逐份通读 DELIVERY_CHECKLIST.md /
    QC_CHECKLIST.md / outputs/*.md 后得出）是：

      · 逃逸事件散落在 4213 行人工记录里，没有任何机器可读形态 → 趋势无法计算；
      · **91 条缺陷里 0 条写过「定位花了多久」**（全仓正则检索 命中 0）→
        第二个指标当时连分母都没有。

    所以这个工具的第一职责不是判对错，是**让第二个指标从今往后能被算出来**：
    `--open` 在现象第一次被观测到的当下盖时间戳，`--rootcause` 在根因写清楚的当下
    再盖一个，两者之差就是定位时长，自动落账。历史条目一律 null，
    **null 不是 0** —— 估算值填进去就等于给自己编一条下降曲线。

三条口径（改这里之前先想清楚，别把可比性改没）：
    1. 台账只收**已进入已发布产物**的缺陷。发版前复核/关卡当场抓到、没上线的不登记，
       所以本工具**不提供「逃逸占比」**（缺分母是有意的，不是遗漏）。
    2. 版本日期由 git 历史推导，不抄人工记录：优先取「APP_VERSION 被改成该版本」那一
       天，缺失时退回提交信息里首次出现 `vX.Y.Z` 的那天，并在输出里标明来源。
       推不出日期的条目不进「每日逃逸」统计，但会计数打印出来。
    3. 「关卡会说假话」（channel=gate_liar）单独一列，不混进线上逃逸数：
       verify_dmg_pyz 无脑 PASS 五十多个版本、探针把全屏前置做成永真，这类不是产品
       缺陷，而是**防线自己失效** —— 它让前两类数字全部失真，所以必须分开看。

用法：
    ./.venv/bin/python tools/quality_trend.py --report
    ./.venv/bin/python tools/quality_trend.py --from-log --days 7   # 现场锚点（数字二的数据源）
    ./.venv/bin/python tools/quality_trend.py --open "现象" --channel online_user \
        [--observed-at "2026-10-09 09:12:03"]   # 从锚点开条时必须传，否则起点会被当成敲命令的那一刻
    ./.venv/bin/python tools/quality_trend.py --rootcause <id>
    ./.venv/bin/python tools/quality_trend.py --close <id> --fixed-version 1.4.46
    ./.venv/bin/python tools/quality_trend.py --gate 1.4.46      # 发版链 3.7 硬拦
    ./.venv/bin/python tools/quality_trend.py --self-test        # 变异自检
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from statistics import median
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
# 演练坑（本轮真实踩到）：走一遍 --open/--rootcause/--close/--round 会**真的**往台账里
# 写一条假缺陷，而台账是唯一的数据源 —— 假条目留在里面，下一轮读出来的两个数字就全是假的。
# 所以路径可以在命令行换（--ledger /tmp/rehearsal.json），演练一律打在临时副本上。
LEDGER = ROOT / "tools" / "escapes_ledger.json"
LEDGER_PATH = LEDGER

# 算作「线上逃逸」的渠道：用户或实机现场能看见的
ESCAPE_CHANNELS = ("online_user", "second_machine", "installed_field")
ALL_CHANNELS = ESCAPE_CHANNELS + ("shipped_unnoticed", "gate_liar")
LINES = ("static", "verify", "locate", "none")
LINE_LABEL = {
    "static": "防线一 静态",
    "verify": "防线二 验证",
    "locate": "防线三 定位",
    "none": "不该指望防线",
}

# 分桶：按版本系列，不是按自然季度（发布节奏从 5 版/天掉到 1.1 版/天，
# 只按版本或只按日子算都会骗人，所以两个分母同时给）
BUCKETS = (
    ("v1.0.x~1.3.x", (0, 0, 0), (1, 4, 0)),
    ("v1.4.0~1.4.15", (1, 4, 0), (1, 4, 16)),
    ("v1.4.16~1.4.30", (1, 4, 16), (1, 4, 31)),
    ("v1.4.31 至今", (1, 4, 31), (99, 99, 99)),
)


def vtuple(text: str | None) -> tuple[int, int, int] | None:
    """1.4.33 → (1,4,33)；带 V/v 前缀或非三段都按缺失处理。"""
    if not text:
        return None
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", text.strip())
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def _git(args: list[str]) -> str:
    """跑 git；失败抛 RuntimeError —— 宁可不出数，也不给假数。"""
    proc = subprocess.run(["git", "-C", str(ROOT), *args],
                          capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：{proc.stderr.strip()[:200]}")
    return proc.stdout


def version_dates() -> dict[str, tuple[str, str]]:
    """版本 → (日期, 来源)。来源 exact=APP_VERSION 进源码那天，msg=提交信息首次出现。"""
    out: dict[str, tuple[str, str]] = {}
    diff = _git(["log", "--date=short", "--format=D|%ad", "-p",
                 "--", "sf_constants.py", "stock_float_window.py"])
    day = ""
    for line in diff.splitlines():
        if line.startswith("D|"):
            day = line[2:]
            continue
        m = re.match(r"\+\s*APP_VERSION\s*=\s*[\"']v?([0-9][0-9.]*|[0-9][0-9.]*)[\"']", line)
        if m and day:
            out.setdefault(m.group(1), (day, "exact"))
    log = _git(["log", "--date=short", "--format=%ad|%s", "--reverse"])
    for line in log.splitlines():
        if "|" not in line:
            continue
        day, subj = line.split("|", 1)
        for m in re.finditer(r"[vV](\d+\.\d+\.\d+)", subj):
            out.setdefault(m.group(1), (day, "msg"))
    return out


def resolve_date(vmap: dict[str, tuple[str, str]], version: str | None) -> tuple[str | None, str | None]:
    hit = vmap.get(version or "")
    if hit:
        return hit[0], hit[1]
    return None, None


def day_span(vmap: dict[str, tuple[str, str]], lo: tuple[int, int, int],
             hi: tuple[int, int, int]) -> tuple[str | None, str | None, int]:
    """桶内版本的自然日跨度与版本数（都由 git 推导，不手填）。"""
    days = []
    n = 0
    for ver, (day, _src) in vmap.items():
        t = vtuple(ver)
        if t is None or not (lo <= t < hi):
            continue
        n += 1
        days.append(day)
    if not days:
        return None, None, n
    return min(days), max(days), n


def load(path: Path | None = None) -> dict[str, Any]:
    src = path or LEDGER_PATH
    with src.open(encoding="utf-8") as fh:
        data: dict[str, Any] = json.load(fh)
    return data


def now_iso() -> str:
    return dt.datetime.now().astimezone().replace(microsecond=0).isoformat(sep=" ")


def locate_minutes(e: dict[str, Any]) -> float | None:
    if e.get("locate_unknown_reason") or "估计" in str(e.get("observed_origin", "")):
        return None
    a, b = e.get("observed_at"), e.get("rootcause_at")
    if not a or not b:
        return None
    try:
        d1 = dt.datetime.fromisoformat(a)
        d2 = dt.datetime.fromisoformat(b)
        if (d1.tzinfo is None) != (d2.tzinfo is None):
            # naive 与 aware 相减会 TypeError：一个手填、一个工具盖戳时不该让报表崩掉，
            # 如实当作「算不出来」，由 validate 单独报那条不一致（v1.4.60 实测踩过）
            return None
    except ValueError:
        return None
    seconds = (d2 - d1).total_seconds()
    return round(seconds / 60.0, 1) if seconds > 0 else None


def bookkeeping_times(entries: list[dict[str, Any]]) -> dict[str, str]:
    """Compare instants, not ISO spellings; cross-endpoint reuse counts too."""
    owners: dict[dt.datetime, set[str]] = {}
    bad: dict[str, str] = {}
    for e in entries:
        eid = str(e.get("id", ""))
        instants = []
        for key in ("observed_at", "rootcause_at"):
            try:
                stamp = dt.datetime.fromisoformat(e.get(key) or "")
            except (ValueError, TypeError):
                continue
            if stamp.tzinfo is not None:
                stamp = stamp.astimezone(dt.timezone.utc)
            stamp = stamp.replace(microsecond=0)
            owners.setdefault(stamp, set()).add(eid)
            instants.append(stamp)
        if len(instants) == 2 and instants[0] == instants[1]:
            bad[eid] = "起点终点相同"
    for stamp, ids in owners.items():
        if len(ids) > 1:
            for eid in ids:
                bad[eid] = f"共用秒级时刻 {stamp.isoformat()}（{', '.join(sorted(ids))}）"
    return bad


def comparable_report(data: dict[str, Any], vmap: dict[str, tuple[str, str]]) -> list[str]:
    policy = data.get("comparison_policy")
    if not policy:
        return ["【可比区间】未声明同等记录口径，以上各桶仅为历史留痕，不得推导改善。"]
    start = vtuple(policy.get("start_version"))
    if start is None:
        return ["【可比区间】起始版本无效，拒绝输出可比率。"]
    lines = [f"【可比区间】v{policy['start_version']} 起为同样细记的候选区间；此前仅历史留痕。",
             "依据：" + str(policy.get("reason", "未提供")),
             "分母为 git 可查版本数（含 Beta/中间号，不冒充已发布版本或用户暴露量）。",
             "窗口 | 线上逃逸 | 有日期逃逸 | 版本数 | 已复盘/版本数 | 自然日 | 每版 | 每30天"]
    # Fixed, non-overlapping version windows; no rolling endpoint chosen to flatter the result.
    observed_versions = [t for v in [*vmap, *(e.get("found_version") for e in data["entries"])]
                         if (t := vtuple(v)) is not None and t >= start]
    windows = set()
    for major, minor, patch in observed_versions:
        first = start[2] if (major, minor) == start[:2] else 0
        lower = first + ((patch - first) // 15) * 15
        windows.add(((major, minor, lower), (major, minor, lower + 15)))
    for lo, hi in sorted(windows):
        versions = {v for v in vmap if lo <= (vtuple(v) or (0, 0, 0)) < hi}
        selected = [e for e in data["entries"] if e.get("channel") in ESCAPE_CHANNELS
                    and lo <= (vtuple(e.get("found_version")) or (0, 0, 0)) < hi]
        if not versions and not selected:
            continue
        reviewed = {r.get("version") for r in data.get("rounds", []) if r.get("reviewed")}
        missing = sorted(versions - reviewed, key=lambda v: vtuple(v) or (0, 0, 0))
        dated = [e for e in selected if e.get("found_version") in versions]
        d0, d1, nver = day_span(vmap, lo, hi)
        days = (dt.date.fromisoformat(d1) - dt.date.fromisoformat(d0)).days + 1 if d0 and d1 else 0
        eligible = bool(nver) and not missing and len(dated) == len(selected)
        rate = f"{len(selected) / nver:.2f}" if eligible else "不可比"
        monthly = f"{len(dated) * 30 / days:.2f}" if eligible and days else "不可比"
        label = f"v{'.'.join(map(str, lo))}~{hi[2] - 1}"
        lines.append(f"{label} | {len(selected)} | {len(dated)} | {nver} | "
                     f"{len(versions & reviewed)}/{nver} | {days or '?'} | {rate} | {monthly}")
        if missing:
            lines.append("  缺复盘版本：" + ", ".join(missing))
        undated = [str(e["id"]) for e in selected if e not in dated]
        if undated:
            lines.append("  缺版本日期条目：" + ", ".join(undated))
        if versions:
            last = max(versions, key=lambda v: vtuple(v) or (0, 0, 0))
            if vtuple(last) != (hi[0], hi[1], hi[2] - 1):
                lines.append(f"  已记录至 v{last}，此号段未满，不把末桶短期起伏当成改善。")
    lines.append("候选区间只控制记账口径；审查强度、用户量与暴露时长未控制，短窗口/末桶不完整，不能据此宣称逃逸下降。")
    return lines


# ---- 日志锚点：让「现象首次可观测时刻」有真数据源 -------------------------------
# 为什么要在日志里找起点（2026-10-09）：台账 44 条里 0 条有定位时长，根因是**过去没人
# 在现象出现的当下记过时间**。但产品侧其实一直在记 —— 防线三的留痕就是带毫秒时间戳
# 与 run id 的行。所以不需要改产品代码，把这批锚点读出来，数字二第一次有了数据源。
# 口径必须说清楚：锚点是**程序检出的时刻**，不是"用户第一次看见"的时刻；
# 用户报障通常晚于它。用锚点当起点会让定位时长**偏短**，所以条目标注 anchor=log。
LOG_LINE_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}) ([0-9a-f]{6})\] "
                         r"(\w+): (.*)$")
LOG_ANCHORS = (
    ("glass_above_main", "re-top: glass above main", "背景层压在内容之上（用户视角的「只剩背景」）",
     "online_user"),
    ("glass_misaligned", "re-top: glass misaligned", "背景层与内容窗真实错位",
     "shipped_unnoticed"),
    ("order_probe_blind", "order probe blind", "置顶复核盲区：两窗此刻不在 OnScreenOnly 列表里，"
     "看门狗看不见这对窗", "shipped_unnoticed"),
    ("geom_resync", "geom resync", "应用侧几何与 WindowServer 不一致（坐标跳变的前置信号）",
     "shipped_unnoticed"),
    ("glass_setup_error", "glass setup error", "玻璃层建层失败/降级", "shipped_unnoticed"),
    ("swallowed", "SWALLOWED", "热路径静默失效留痕", "shipped_unnoticed"),
)
# ⚠ 这份 needle 表与产品侧 `sf_logging.GLASS_ANCHORS` 必须同键同 needle：
# 两边不一致时，最坏的失效是**扫不到却报"没有锚点"**（又一枚会说假话的检查）。
# 不 import sf_logging 是因为它会拖进 PyQt6 —— 这个工具必须在没有 GUI 的机器上也能跑。
# 漂移由 --from-diag 在**运行时**报出来（诊断包里的键这里不认识就大声警告），
# 不靠读源码断言（AGENTS §6 第 2 条）。


def from_diag_report(zip_path: Path) -> str:
    """读用户回传的诊断包（zip 里的 glass.json + debug.log），把现场片段摊开。

    这条是"逃逸快速定位"的正门：现场在别人的机器上时，本机再怎么复现都是二手。
    v1.4.46 起诊断包带 glass.json（合成顺序 / 真实错位 / 盲区 / 每种失效最后一次
    的年龄 / 最近 30 分钟锚点）。**旧产物导出的包里没有这一项**，那时必须如实说
    "这个包没带现场"，而不是扫出个空表当作"没发生"。
    """
    lines = [f"诊断包现场 · {zip_path.name}"]
    if not zip_path.exists():
        raise RuntimeError(f"找不到诊断包 {zip_path}")
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        payload = {n: z.read(n) for n in names}
    known = {a[0] for a in LOG_ANCHORS}
    if "glass.json" not in payload:
        lines.append("  这个包里**没有 glass.json** —— 它由 v1.4.45 及更早的产物导出，"
                     "那一版还没有玻璃层现场与锚点。")
        lines.append("  「扫不出锚点」在这种情况下不等于「没发生过」；"
                     "要现场数据，得等装机版升到带 glass.json 的那一版再导一次。")
    else:
        data = json.loads(payload["glass.json"].decode("utf-8", "replace"))
        lines.append("  产物 v%s run=%s native_cocoa=%s 玻璃层在=%s 跟随定时器在=%s"
                     % (data.get("version"), data.get("run"), data.get("native_cocoa"),
                        data.get("glass_present"), data.get("follow_timer_active")))
        st = data.get("real_state")
        if st is None:
            lines.append("  此刻合成顺序：%s" % (data.get("real_state_note") or "问不出来"))
        else:
            lines.append("  此刻合成顺序：玻璃在上=%s 真实错位=%s px"
                         % (st.get("glass_on_top"), st.get("desync_px")))
        if data.get("retop_last"):
            for reason, rec in sorted(data["retop_last"].items()):
                lines.append("  最后一次「%s」距今 %.1f 秒，其间压掉 %d 条同类"
                             % (reason, rec.get("age_sec", 0.0), rec.get("suppressed", 0)))
        anchors = data.get("anchors") or {}
        got = anchors.get("anchors") or []
        if anchors.get("error"):
            lines.append("  锚点扫描失败：%s" % anchors["error"])
        elif not got:
            lines.append("  最近 %s 分钟没有锚点（读了 %s 字节，truncated=%s，"
                         "未解析行 %s）—— 这三件都得如实报，空表不等于没出事"
                         % (anchors.get("window_minutes"), anchors.get("read_bytes"),
                            anchors.get("truncated"), anchors.get("unparsed_lines")))
        else:
            lines.append("  %s" % (anchors.get("note") or ""))
            for a in got:
                if a.get("key") not in known:
                    lines.append("  ⚠ 产品侧新增了一类锚点「%s」，本工具的 needle 表还没跟上 —— "
                                 "再往下读就会**扫不到却报没有锚点**" % a.get("key"))
                lines.append("  %-20s %4d 行  %s → %s  run=%s"
                             % (a.get("key"), a.get("hits", 0), a.get("first_seen"),
                                a.get("last_seen"), ",".join(a.get("runs") or [])))
                lines.append("       %s" % (a.get("desc") or ""))
                lines.append("    开条（起点直接用锚点时刻）：tools/quality_trend.py --open \"%s\" "
                             "--channel %s --observed-at \"%s\" --component 玻璃层与层级 "
                             "--source \"diag:%s run=%s\""
                             % (a.get("desc"), _diag_channel(a.get("key")),
                                str(a.get("first_seen", ""))[:19], a.get("key"),
                                a.get("first_run")))
    log_names = [n for n in payload if n.endswith("debug.log")]
    if log_names:
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "debug.log"
            f.write_bytes(payload[log_names[0]])
            eps, meta = scan_log(f, days=30)
            lines.append("  包里的 debug.log 复核（30 天窗口，%d 个片段、%d 行未解析）："
                         % (len(eps), meta["unparsed"]))
            for e in eps[:8]:
                lines.append("    %-20s run=%-8s %5d 行 持续 %8.1f 秒 起点 %s"
                             % (e["anchor"], e["run"], e["hits"], e["span_sec"],
                                e["first"].isoformat(timespec="milliseconds")))
    return "\n".join(lines)


def _diag_channel(key):
    for k, _needle, _desc, ch in LOG_ANCHORS:
        if k == key:
            return ch
    return "shipped_unnoticed"


def default_log_path() -> Path:
    env = os.environ.get("SF_LOG_PATH")
    if env:
        return Path(env)
    return Path.home() / "Library" / "Application Support" / "StockFloatTargetPrice" / "debug.log"


def scan_log(path: Path, days: int) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """按 (run id, 锚点类) 归并出每一次"现场片段"，起点=该类第一行的时间戳。

    返回 (片段列表, 元信息)。元信息里的 unparsed 是要如实报的：**没解析的行不等于没有事**，
    多行 traceback 与非标准行都会落在这里，藏着不回声就等于骗人。
    """
    if not path.exists():
        raise RuntimeError(f"找不到日志 {path}（滚动后可能已归到 .1，用 --log-path 指定）")
    cutoff = dt.datetime.now() - dt.timedelta(days=days)
    episodes: dict[tuple[str, str], dict[str, Any]] = {}
    unparsed = 0
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = LOG_LINE_RE.match(raw)
        if not m:
            if raw.strip():
                unparsed += 1
            continue
        ts = dt.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S.%f")
        if ts < cutoff:
            continue
        run, msg = m.group(2), m.group(4)
        for key, needle, desc, channel in LOG_ANCHORS:
            if needle in msg:
                ep = episodes.setdefault((run, key), {
                    "run": run, "anchor": key, "desc": desc, "channel": channel,
                    "first": ts, "last": ts, "hits": 0, "sample": msg[:120]})
                ep["hits"] += 1
                ep["last"] = max(ep["last"], ts)
                break
    out = sorted(episodes.values(), key=lambda e: e["first"], reverse=True)
    for e in out:
        e["span_sec"] = round((e["last"] - e["first"]).total_seconds(), 1)
    return out, {"unparsed": unparsed}


def from_log_report(path: Path, days: int) -> str:
    eps, meta = scan_log(path, days)
    lines = [f"日志锚点扫描 · {path}（最近 {days} 天，起点口径=程序检出时刻，"
             f"不是用户第一次看见）"]
    if not eps:
        lines.append("  这个窗口内没有任何锚点 —— 现行产物没检出过这一族现象（或日志已轮转掉）。")
    else:
        lines.append("%-20s %-8s %6s %9s  %s" % ("锚点", "run", "次数", "持续秒", "首次可观测时刻"))
        for e in eps:
            lines.append("%-20s %-8s %6d %9.1f  %s"
                         % (e["anchor"], e["run"], e["hits"], e["span_sec"],
                            e["first"].isoformat(timespec="milliseconds")))
        by: dict[str, list[dict[str, Any]]] = {}
        for e in eps:
            by.setdefault(e["anchor"], []).append(e)
        lines.append("  ---")
        desc_of = {a[0]: a[2] for a in LOG_ANCHORS}
        for key, group in sorted(by.items(), key=lambda kv: -sum(x["hits"] for x in kv[1])):
            hits = sum(x["hits"] for x in group)
            longest = max(group, key=lambda x: x["span_sec"])
            lines.append("  %-20s %d 个片段 / %d 行 · 最长一段 %.1f 秒（run %s）· %s"
                         % (key, len(group), hits, longest["span_sec"], longest["run"], desc_of[key]))
        lines.append("  ---")
        lines.append("  把某个片段变成台账条目（起点直接用锚点时刻，别用现在的钟）：")
        for e in eps[:3]:
            lines.append("    tools/quality_trend.py --open \"%s\" --channel %s "
                         "--observed-at \"%s\" --component 玻璃层与层级 --source \"log:%s run=%s\""
                         % (e["desc"], e["channel"],
                            e["first"].isoformat(timespec="seconds"), e["anchor"], e["run"]))
    if meta["unparsed"]:
        lines.append("  注：%d 行不是标准行格式（多行 traceback 等），未计入锚点 —— "
                     "「没锚点」不等于「没出事」。" % meta["unparsed"])
    return "\n".join(lines)



def latent_versions(vmap: dict[str, tuple[str, str]], introduced: str | None,
                    found: str | None) -> int | None:
    """引入→发现之间隔了几个版本（git 推导的可查证事实，不是定位时长）。"""
    ti, tf = vtuple(introduced), vtuple(found)
    if ti is None or tf is None:
        return None
    return sum(1 for v in vmap if (lambda t: t is not None and ti <= t < tf)(vtuple(v)))


def report(data: dict[str, Any]) -> str:
    vmap = version_dates()
    entries = data["entries"]
    lines = []
    lines.append("逃逸台账 · tools/escapes_ledger.json（schema %s，更新于 %s）"
                 % (data.get("schema"), data.get("updated")))
    lines.append("口径：只登记已进入已发布产物的缺陷；复核/关卡当场抓到的不在此列，"
                 "故本表**没有**「逃逸占比」这一分母。")

    strict = [e for e in entries if e["channel"] in ESCAPE_CHANNELS]
    unnoticed = [e for e in entries if e["channel"] == "shipped_unnoticed"]
    liars = [e for e in entries if e["channel"] == "gate_liar"]

    # ---- 数字一：线上逃逸数，按桶给两个分母 ----
    lines.append("")
    lines.append("【数字一】线上逃逸数（用户或实机现场能看见的：online_user / second_machine / installed_field）")
    lines.append("%-16s %6s %8s %8s %10s %10s" % ("桶", "逃逸", "版本数", "自然日", "每版", "每 30 天"))
    for name, lo, hi in BUCKETS:
        sel = [e for e in strict if (vtuple(e["found_version"]) or (0, 0, 0)) >= lo
               and (vtuple(e["found_version"]) or (0, 0, 0)) < hi]
        d0, d1, nver = day_span(vmap, lo, hi)
        days = 0
        if d0 and d1:
            days = (dt.date.fromisoformat(d1) - dt.date.fromisoformat(d0)).days + 1
        per_ver = ("%.2f" % (len(sel) / nver)) if nver else "-"
        dated_sel = [e for e in sel if resolve_date(vmap, e["found_version"])[0]]
        per_30 = ("%.2f" % (len(dated_sel) * 30 / days)) if days else "-"
        lines.append("%-16s %6d %8d %8s %10s %10s"
                     % (name, len(sel), nver, days or "?", per_ver, per_30))
    undated = [str(e["id"]) for e in strict if not resolve_date(vmap, e["found_version"])[0]]
    dated = len(strict) - len(undated)
    lines.append("注一：以上旧桶只作历史留痕，记录密度不同，不可跨桶比较改善；每30天也不能消除记录偏差。")
    lines.append("注二：记录密度本身在变 —— v1.4.x 每版都有交付节，v1.2/1.3 只有批次级留痕，"
                 "「发现得更多」与「逃逸得更多」在旧桶里分不开。台账共 %d 条，其中 %d/%d 条能推出发现日期。"
                 % (len(entries), dated, len(strict)))
    if undated:
        lines.append("     推不出发现日期（不进每 30 天那列）：%s" % ", ".join(undated))
    lines.append("注三：另有 %d 条【已上线但用户看不见或没人报】（S 开头，如抬层整段从未执行、"
                 "清单超时导致常年不告知新版本），%d 条【关卡自己会说假话】（G 开头）—— "
                 "后者不占线上逃逸数，但它让前两个数字失真。" % (len(unnoticed), len(liars)))
    lines.extend(comparable_report(data, vmap))

    # ---- 数字二：平均定位时长 ----
    lines.append("")
    lines.append("【数字二】平均定位时长（现象首次可观测 → 根因写清楚）")
    bookkeeping = bookkeeping_times(entries)
    timed = [e for e in entries if str(e["id"]) not in bookkeeping
             and locate_minutes(e) is not None]
    unknown_time = [e for e in entries if e not in timed]
    lines.append(f"当场盖戳/可追溯证据的真时长：{len(timed)} 条 | 时刻未知：{len(unknown_time)} 条（含记账时刻与待定位）。")
    lines.append("有效区间沿用原账现场/日志声明；未触发异常不等于本轮逐条重新认证，终点来源未声明的在下方点名。")
    for e in unknown_time:
        eid = str(e["id"])
        reason = bookkeeping.get(eid) or e.get("locate_unknown_reason") or "缺起点或终点"
        if eid in bookkeeping or e.get("timestamp_audit"):
            lines.append(f"  {eid}：记账时刻，不是定位时长；已剔除平均值。{reason}")
    only_start = [e for e in entries
                  if e.get("observed_at") and not e.get("rootcause_at")]
    if timed:
        vals = [locate_minutes(e) for e in timed]
        real = [v for v in vals if v is not None]
        lines.append("可计算条目 %d 条，平均 %.1f 分钟，中位 %.1f 分钟，最长 %.1f 分钟。"
                     % (len(timed), sum(real) / len(real),
                        median(real), max(real)))
        for e in timed:
            lines.append(f"  {e['id']}：{locate_minutes(e):.1f} 分钟；起点来源={e.get('observed_origin', '旧账未声明')}；"
                         f"终点来源={e.get('rootcause_origin', '旧账未声明，未重新核验')}")
    else:
        lines.append("可计算条目 **0** 条（台账 %d 条里有效起终点区间为 0）—— "
                     "这个指标目前**无法判断是否下降**，不是「没下降」，是没有分母。"
                     % len(entries))
    if only_start:
        lines.append("已有起点、还差终点（根因写清的当下盖 --rootcause）：%s"
                     % ", ".join(str(e["id"]) for e in only_start))
    if not timed:
        lines.append("从本轮起用 --open / --from-log 拿起点、--rootcause 拿终点，"
                     "两轮之后这个数才第一次可比。")
    unknown = [e for e in entries
               if locate_minutes(e) is None and not e.get("locate_unknown_reason")]
    if unknown:
        lines.append("⚠ %d 条既没有时刻戳、也没写「为什么记不到」：%s"
                     % (len(unknown), ", ".join(str(e["id"]) for e in unknown)))

    # ---- 防线失守分布：下一轮该补哪道 ----
    lines.append("")
    lines.append("【防线失守分布】复盘逃逸 bug 问的就是这个 —— 数条目按 component 与 failed_lines")
    by_comp: dict[str, int] = {}
    for e in entries:
        by_comp[e.get("component", "?")] = by_comp.get(e.get("component", "?"), 0) + 1
    for comp, cnt in sorted(by_comp.items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append("  %-14s %2d 条" % (comp, cnt))
    by_line: dict[str, int] = {}
    for e in entries:
        for fl in e.get("failed_lines", []):
            by_line[fl] = by_line.get(fl, 0) + 1
    lines.append("  ---")
    for fl in LINES:
        if fl in by_line:
            lines.append("  %-12s 失守 %2d 次" % (LINE_LABEL[fl], by_line[fl]))
    no_defense = [e["id"] for e in strict if not e.get("defense_added")]
    lines.append("  已上线逃逸里 %d 条至今没登记「补上了什么防线」：%s"
                 % (len(no_defense), ", ".join(no_defense) if no_defense else "无"))
    risk: dict[str, list[str]] = {}
    for e in entries:
        r = e.get("recurrence_risk")
        if r:
            risk.setdefault(r, []).append(str(e["id"]))
    if risk:
        lines.append("  全台账**复发风险判定**（含有防线落点条目，不把登记了防线当成保得住）：")
        for r, ids in sorted(risk.items(), key=lambda kv: ({"高": 0, "中": 1, "低": 2}.get(kv[0], 3), -len(kv[1]))):
            lines.append("    %-14s %d 条：%s" % (r, len(ids), ", ".join(ids)))
        for e in entries:
            if e.get("recurrence_risk") == "高":
                lines.append(f"    下一轮 {e['id']}：{e.get('recurrence_next_action', '需补具体行为判据与反向对照')}")
    unrated = [str(e["id"]) for e in entries if not e.get("recurrence_risk")]
    lines.append(f"  未评级 {len(unrated)} 条：" + (", ".join(unrated) or "无"))

    # ---- 潜伏跨度：可查证的间接事实 ----
    lines.append("")
    lines.append("【潜伏跨度】引入→发现之间递增了多少个版本号（git 推导，含未发布的中间号，"
                 "**不是定位时长**，只用来排「哪类缺陷活得最久」）")
    rows = []
    for e in entries:
        lv = latent_versions(vmap, e.get("introduced_version"), e.get("found_version"))
        if lv:
            rows.append((lv, str(e["id"]), e["symptom"][:34]))
    rows.sort(reverse=True)
    for lv, eid, sym in rows[:8]:
        lines.append("  %3d 个版本  %s  %s" % (lv, eid, sym))
    if not rows:
        lines.append("  （无可用条目）")
    lines.append("  另：引入版本写不出来的 %d 条（原文只写「潜伏几十个版本」这类），不计入上面排序。"
                 % sum(1 for e in entries if not e.get("introduced_version")))
    lines.append("  注：这一列数的是**进过源码的版本号**（含 Beta 与中间号），比交付记录里"
                 "「约 60 个版本」这类人工口径大，两个口径别混用。")

    # ---- 复盘闭环 ----
    lines.append("")
    rounds = data.get("rounds", [])
    lines.append("【复盘闭环】rounds 记录 %d 条（发版链 3.7 步要求每个新版本先写复盘声明才准出包）"
                 % len(rounds))
    for r in rounds[-5:]:
        lines.append("  v%s %s 本轮逃逸 %s 条 · 补的防线：%s"
                     % (r.get("version"), r.get("date"), r.get("escapes_this_round"),
                        "; ".join(r.get("defense_added") or []) or r.get("zero_reason", "-")))
    return "\n".join(lines)


def validate(data: dict[str, Any], target_version: str | None = None) -> list[str]:
    """返回错误清单；空表=通过。宁可多报，不要静默放过。

    这里**不查 git**：闸门要在没有网络/没有完整历史的工作副本上也能给出同一结论，
    版本日期只用于 --report 的趋势分桶。
    """
    errs: list[str] = []
    entries = data.get("entries", [])
    for eid, reason in bookkeeping_times(entries).items():
        errs.append(f"{eid}: 记账时刻，不是定位时长；{reason}；已剔除平均值，请凭证据修正或退回未知")
    audited = set(data.get("recurrence_review", {}).get("entry_ids", []))
    missing_ids = audited - {str(e.get("id", "")) for e in entries}
    if missing_ids:
        errs.append("已审查条目丢失：" + ", ".join(sorted(missing_ids)))
    seen: set[str] = set()
    for e in entries:
        eid = str(e.get("id", ""))
        if not eid:
            errs.append("有条目缺 id")
            continue
        if eid in seen:
            errs.append(f"{eid}: id 重复")
        seen.add(eid)
        if eid in audited:
            if e.get("recurrence_risk") not in ("高", "中", "低"):
                errs.append(f"{eid}: 缺有效高/中/低复发评级")
            for key in ("recurrence_guard_refs", "recurrence_liar_check", "recurrence_next_action"):
                if not e.get(key):
                    errs.append(f"{eid}: 复发审查缺 {key}")
        if e.get("channel") not in ALL_CHANNELS:
            errs.append(f"{eid}: channel「{e.get('channel')}」不在口径名单 {ALL_CHANNELS}")
        fls = e.get("failed_lines") or []
        if not fls:
            errs.append(f"{eid}: failed_lines 为空 —— 逃逸没归到任何一道防线，等于没复盘")
        bad = [x for x in fls if x not in LINES]
        if bad:
            errs.append(f"{eid}: failed_lines 含未知值 {bad}")
        if not (e.get("symptom") or "").strip():
            errs.append(f"{eid}: 缺 symptom")
        if not (e.get("source") or "").strip():
            errs.append(f"{eid}: 缺 source（出处），条目无法复核")
        if not e.get("found_version"):
            errs.append(f"{eid}: 缺 found_version")
        # 时刻戳的自洽：有终点必须有起点，起点不能晚于终点
        ob, ra = e.get("observed_at"), e.get("rootcause_at")
        if ra and not ob:
            errs.append(f"{eid}: 有 rootcause_at 却没有 observed_at —— 定位时长无法成立，"
                        "疑似回填了终点时刻")
        if ob and ra:
            try:
                a, b = dt.datetime.fromisoformat(ra), dt.datetime.fromisoformat(ob)
                if (a.tzinfo is None) != (b.tzinfo is None):
                    # naive 与 aware 相比会 TypeError；闸门崩掉比报一条人话错误更糟
                    # （本轮实测踩过：手填 --observed-at 写成不带时区的 ISO 串）
                    errs.append(f"{eid}: observed_at 与 rootcause_at 一个带时区一个不带，"
                                "定位时长无法比较 —— 统一按 now_iso() 的带偏移形状写")
                elif a < b:
                    errs.append(f"{eid}: rootcause_at 早于 observed_at")
            except ValueError:
                errs.append(f"{eid}: 时刻戳不是 ISO 格式")
        elif ob:
            try:
                dt.datetime.fromisoformat(ob)
            except ValueError:
                errs.append(f"{eid}: observed_at「{ob}」不是 ISO 时刻 —— "
                            f"用日志锚点开条时请照抄 scan 输出的格式")
        if locate_minutes(e) is None and not e.get("locate_unknown_reason"):
            errs.append(f"{eid}: 既无定位时刻、也未说明为何记不到（null 必须带理由）")
        fv, xv = vtuple(e.get("found_version")), vtuple(e.get("fixed_version"))
        if fv and xv and xv < fv:
            errs.append(f"{eid}: fixed_version {e.get('fixed_version')} 早于 found_version "
                        f"{e.get('found_version')}")
        here = target_version and str(e.get("found_version")) == target_version
        fixed_here_flag = target_version and str(e.get("fixed_version")) == target_version
        if e.get("channel") in ESCAPE_CHANNELS and not e.get("defense_added") \
                and "none" not in fls and (here or fixed_here_flag):
            # 只硬拦**本轮相关**的逃逸：早期条目当年确实没补防线，那是历史事实，
            # 由 --report 作为「遗留」列出，不是本轮发版的理由。
            errs.append(f"{eid}: 本轮发现/收口的线上逃逸但 defense_added 为空 —— "
                        "防线没补，下一轮还会再来一条")

    if target_version:
        rounds = [r for r in data.get("rounds", []) if str(r.get("version")) == target_version]
        if not rounds:
            errs.append(f"rounds 里没有 v{target_version} 的复盘声明 —— "
                        "要么本轮没复盘就发版，要么台账没同步")
        else:
            r = rounds[-1]
            if not r.get("reviewed"):
                errs.append(f"v{target_version} 的复盘声明 reviewed 不是 true")
            esc = r.get("escapes_this_round")
            if not isinstance(esc, int) or esc < 0:
                errs.append(f"v{target_version} 没填 escapes_this_round（非负整数，0 也要明写 0）")
            if esc == 0 and not (r.get("zero_reason") or "").strip():
                errs.append(f"v{target_version} 报 0 逃逸但没写为什么是 0（真 0 还是没人看）")
            if isinstance(esc, int) and esc == 0:
                # 堵住反向的空子：台账里明明有本轮发现的线上逃逸，声明却写 0。
                # gate_liar（关卡自己说假话）不计入逃逸数，所以这里只查 ESCAPE_CHANNELS。
                clash = [str(x["id"]) for x in entries
                         if str(x.get("found_version")) == target_version
                         and x.get("channel") in ESCAPE_CHANNELS]
                if clash:
                    errs.append(f"v{target_version} 声明 0 逃逸，但台账里 {clash} 的发现版本"
                                "就是本号 —— 报 0 之前先想清楚是真 0 还是没看")
            if esc and esc > 0 and not (r.get("defense_added") or []):
                errs.append(f"v{target_version} 有 {esc} 条逃逸却没登记补了哪道防线")
        # 对账只看**线上逃逸**那几个渠道：关卡说假话（G）与已上线但没人报（S）各有自己的
        # 口径，混进来会让"本轮 0 逃逸"这种如实的声明永远被判红 —— 一道总是红的闸门
        # 和一道永不红的闸门一样没用，最后被人绕过去。
        strict_here = [e for e in entries if e.get("channel") in ESCAPE_CHANNELS]
        fixed_here = [str(e["id"]) for e in strict_here
                      if str(e.get("fixed_version")) == target_version]
        here_ids = [str(e["id"]) for e in strict_here
                    if str(e.get("found_version")) == target_version
                    or str(e.get("fixed_version")) == target_version]
        r0 = rounds[-1] if rounds else {}
        declared = r0.get("escapes_this_round")
        if isinstance(declared, int) and len(fixed_here) > declared:
            errs.append(f"v{target_version} 声明本轮逃逸 {declared} 条，但台账里 fixed_version "
                        f"落在本号的有 {len(fixed_here)} 条（{fixed_here}）—— 声明比台账少")
        if isinstance(declared, int) and declared > 0 and not here_ids:
            # 允许「本轮发现、下轮修」：那种条目 found_version 就是本号，所以这里查的是
            # 台账里**完全没有任何**本轮痕迹 —— 那就是凭口头报数。
            errs.append(f"v{target_version} 声明本轮逃逸 {declared} 条，但台账里没有任何一条的"
                        "发现版本或修复版本落在本号 —— 声明没有依据")
    return errs


def print_gate(data: dict[str, Any], target_version: str | None) -> int:
    archived = [str(e["id"]) for e in data.get("entries", [])
                if e.get("timestamp_audit") and locate_minutes(e) is None]
    if archived:
        print("   记账时刻，不是定位时长（旧值已归档，平均值剔除）：" + ", ".join(archived))
    errs = validate(data, target_version)
    if errs:
        print("!! 逃逸复盘闸门未过（%d 条）：" % len(errs))
        for m in errs:
            print("   ·", m)
        print("!! 台账条目可以「记不到时长」，但不能「没归防线、没说为什么记不到」。")
        return 1
    print("   逃逸复盘闸门通过：条目自洽" + ("" if not target_version
                                            else f"，且 v{target_version} 已有复盘声明"))
    return 0


def cmd_open(args: argparse.Namespace) -> int:
    data = load()
    eid = args.id or _next_id(data, args.prefix)
    entry: dict[str, Any] = {
        "id": eid,
        "symptom": args.symptom,
        "component": args.component or "未归类",
        "channel": args.channel,
        "introduced_version": args.introduced,
        "introduced_note": args.note,
        "found_version": args.found_version or args.version or None,
        "fixed_version": None,
        "failed_lines": [],
        "why_missed": None,
        "defense_added": None,
        # 起点默认=现在；从日志锚点开条时必须显式传 --observed-at，
        # 否则"定位时长"会把「从敲命令起」当成「从现象出现起」，是个系统性低估。
        "observed_at": args.observed_at or now_iso(),
        "observed_origin": "log" if args.observed_at else "now",
        "rootcause_at": None,
        "locate_unknown_reason": None,
        "source": args.source or "本轮现场",
        "evidence": "verified",
    }
    data["entries"].append(entry)
    _save(data)
    print(f"已开条 {eid}（observed_at={entry['observed_at']} "
          f"来源={entry['observed_origin']}）—— 根因清楚后跑 --rootcause {eid} 盖第二个戳")
    return 0


def cmd_rootcause(args: argparse.Namespace) -> int:
    data = load()
    e = _find(data, args.entry_id)
    if e.get("rootcause_at"):
        print(f"!! {e['id']} 已有根因戳，拒绝用本次记账时刻覆盖原值")
        return 1
    e["rootcause_at"] = now_iso()
    e["rootcause_origin"] = "now"
    if not e.get("observed_at"):
        e["locate_unknown_reason"] = "开条时没在现场盖 observed_at，起点无法追溯"
    else:
        e["locate_unknown_reason"] = None
    if args.why_missed:
        e["why_missed"] = args.why_missed
    _save(data)
    mins = locate_minutes(e)
    print(f"{e['id']} 已盖 rootcause_at，定位时长 = "
          + (f"{mins} 分钟" if mins is not None else "无法计算（缺起点戳）"))
    return 0


def cmd_close(args: argparse.Namespace) -> int:
    data = load()
    e = _find(data, args.entry_id)
    e["fixed_version"] = args.fixed_version
    e["failed_lines"] = args.failed_lines
    e["defense_added"] = args.defense
    if args.component:
        e["component"] = args.component
    _save(data)
    print(f"{e['id']} 已收口：v{args.fixed_version} · 失守防线 {args.failed_lines} · 补的防线落点已登记")
    return 0


def cmd_round(args: argparse.Namespace) -> int:
    data = load()
    rec: dict[str, Any] = {
        "version": args.version,
        "date": dt.date.today().isoformat(),
        "reviewed": True,
        "escapes_this_round": args.escapes,
        "defense_added": args.defense or [],
        "zero_reason": args.zero_reason,
    }
    data["rounds"] = [r for r in data.get("rounds", []) if str(r.get("version")) != args.version]
    data["rounds"].append(rec)
    _save(data)
    print(f"已写入 v{args.version} 的复盘声明")
    return 0


def _next_id(data: dict[str, Any], prefix: str) -> str:
    nums = []
    for e in data["entries"]:
        m = re.fullmatch(rf"{prefix}(\d+)", str(e.get("id", "")))
        if m:
            nums.append(int(m.group(1)))
    return f"{prefix}{max(nums) + 1 if nums else 1:02d}"


def _find(data: dict[str, Any], eid: str) -> dict[str, Any]:
    for e in data["entries"]:
        if str(e.get("id")) == eid:
            return e
    raise SystemExit(f"台账里没有 id={eid}")


def _save(data: dict[str, Any]) -> None:
    data["updated"] = dt.date.today().isoformat()
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    LEDGER_PATH.write_text(text, encoding="utf-8")
    # 本仓实测坑：写文件可能返回空，读回再判一次
    back = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    if len(back["entries"]) != len(data["entries"]):
        raise SystemExit("台账写入后读回条目数不一致，拒绝继续")


def self_test() -> int:
    """变异自检：一条永不失败的检查等于没有检查（本仓为这句话栽过至少四次）。"""
    base: dict[str, Any] = {
        "schema": 1, "updated": "2026-10-09",
        "entries": [{
            "id": "T1", "symptom": "样例现象", "component": "样例件",
            "channel": "online_user", "introduced_version": "1.4.40",
            "introduced_note": None, "found_version": "1.4.46",
            "fixed_version": "1.4.46", "failed_lines": ["verify"],
            "why_missed": "样例", "defense_added": "样例防线",
            "observed_at": "2026-10-09 10:00:00+08:00",
            "rootcause_at": "2026-10-09 10:37:00+08:00",
            "locate_unknown_reason": None, "source": "self-test", "evidence": "verified",
        }],
        "rounds": [{
            "version": "1.4.46", "date": "2026-10-09", "reviewed": True,
            "escapes_this_round": 1, "defense_added": ["样例防线"], "zero_reason": None,
        }],
    }
    ok = 0
    if locate_minutes(base["entries"][0]) != 37.0:
        print("FAIL 定位时长算法：期望 37.0 分钟")
        return 1
    ok += 1
    clean = validate(json.loads(json.dumps(base)), "1.4.46")
    if clean:
        print("FAIL 干净样本被判红：", clean)
        return 1
    ok += 1
    # 放宽项必须真的是放宽：早期条目当年没补防线，不该成为今天发版的理由
    legacy = json.loads(json.dumps(base))
    legacy["entries"][0]["found_version"] = "1.4.40"
    legacy["entries"][0]["fixed_version"] = "1.4.41"
    legacy["entries"][0]["defense_added"] = None
    legacy["rounds"][0]["escapes_this_round"] = 0
    legacy["rounds"][0]["zero_reason"] = "本轮为纯测试轮，未出包也未改产品行为"
    legacy["rounds"][0]["defense_added"] = []
    if validate(legacy, "1.4.46"):
        print("FAIL 历史遗留条目被硬拦了（口径应当只管本轮）：", validate(legacy, "1.4.46"))
        return 1
    ok += 1
    mutations = [
        ("归不到任何防线", lambda d: d["entries"][0].__setitem__("failed_lines", [])),
        ("channel 用错口径", lambda d: d["entries"][0].__setitem__("channel", "test_only")),
        ("没有出处", lambda d: d["entries"][0].__setitem__("source", "")),
        ("本轮逃逸没补防线", lambda d: d["entries"][0].__setitem__("defense_added", None)),
        ("记不到时长又不说原因", lambda d: [d["entries"][0].__setitem__("observed_at", None),
                                          d["entries"][0].__setitem__("rootcause_at", None),
                                          d["entries"][0].__setitem__("locate_unknown_reason", None)]),
        ("回填终点戳（起点为空）", lambda d: d["entries"][0].__setitem__("observed_at", None)),
        ("时刻倒挂", lambda d: d["entries"][0].__setitem__(
            "rootcause_at", "2026-10-09 09:00:00+08:00")),
        ("起终点相同的记账时刻", lambda d: d["entries"][0].__setitem__(
            "rootcause_at", d["entries"][0]["observed_at"])),
        ("多条共用秒级起点", lambda d: d["entries"].append(
            {**d["entries"][0], "id": "T2", "rootcause_at": "2026-10-09 10:38:00+08:00"})),
        ("修复版本早于发现版本", lambda d: d["entries"][0].__setitem__("fixed_version", "1.4.10")),
        ("本轮没有复盘声明", lambda d: d.__setitem__("rounds", [])),
        ("复盘声明没勾选已复核", lambda d: d["rounds"][0].__setitem__("reviewed", False)),
        ("报 0 逃逸但不说为什么是 0", lambda d: [
            d["rounds"][0].__setitem__("escapes_this_round", 0),
            d["rounds"][0].__setitem__("zero_reason", None)]),
        ("声明比台账少（漏登记）", lambda d: d["rounds"][0].__setitem__("escapes_this_round", 0)),
        ("声明有逃逸但台账里没有对应条目", lambda d: [
            d.__setitem__("entries", []), d["rounds"][0].__setitem__("escapes_this_round", 2)]),
        ("报 0 但台账里本号就有逃逸", lambda d: [
            d["rounds"][0].__setitem__("escapes_this_round", 0),
            d["rounds"][0].__setitem__("zero_reason", "真 0"),
            d["entries"][0].__setitem__("found_version", "1.4.46")]),
    ]
    for name, mutate in mutations:
        d = json.loads(json.dumps(base))
        mutate(d)
        errs = validate(d, "1.4.46")
        if not errs:
            print(f"FAIL 变异未被抓到：{name}")
            return 1
    ok += len(mutations)
    # 演练隔离必须是真的隔离：--ledger 指到临时副本开一条，真台账一个字节都不许变
    # （本轮真实踩过：演练开出来的 N01 留在台账里，平均定位时长立刻变成 0.2 分钟的假数）
    real_bytes = LEDGER.read_bytes()
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / "rehearsal.json"
        tmp.write_bytes(real_bytes)
        n_before = len(load(tmp)["entries"])
        rc = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--ledger", str(tmp),
                             "--open", "演练条目", "--prefix", "Z", "--channel", "online_user",
                             "--found-version", "9.9.9", "--source", "self-test"],
                            capture_output=True, text=True, check=False).returncode
        n_after = len(load(tmp)["entries"])
        if rc != 0 or n_after != n_before + 1:
            print(f"FAIL --ledger 演练：rc={rc} 条目 {n_before}→{n_after}")
            return 1
    if LEDGER.read_bytes() != real_bytes:
        print("FAIL --ledger 演练把真台账改了 —— 演练等于污染唯一数据源")
        return 1
    ok += 2
    # 锚点扫描自己也是一条判据：喂一份形状已知的日志，片段数/起点/持续/未解析行数
    # 必须全对得上。一条只会说"没锚点"的扫描器，就是又一枚永不失败的检查。
    sample = (
        "[2026-10-07 09:00:00.100 a1b2c3] INFO: STARTUP v1.4.45 run=a1b2c3\n"
        "[2026-10-07 09:00:01.200 a1b2c3] WARNING: re-top: glass above main ×3\n"
        "[2026-10-07 09:00:02.300 a1b2c3] WARNING: re-top: glass above main\n"
        "[2026-10-07 09:00:03.400 a1b2c3] WARNING: order probe blind: pair not both on-screen\n"
        "[2026-10-08 10:00:04.500 d4e5f6] ERROR: SWALLOWED sf_glass._force_top: AttributeError\n"
        "这一行不是标准格式（traceback 续行）\n"
        "[2020-01-01 00:00:00.000 a1b2c3] WARNING: re-top: glass above main\n"
    )
    with tempfile.TemporaryDirectory() as td2:
        lg = Path(td2) / "debug.log"
        lg.write_text(sample, encoding="utf-8")
        eps, meta = scan_log(lg, 30)
        want = {("a1b2c3", "glass_above_main"): 2,
                ("a1b2c3", "order_probe_blind"): 1,
                ("d4e5f6", "swallowed"): 1}
        got = {(e["run"], e["anchor"]): e["hits"] for e in eps}
        if got != want:
            print("FAIL 锚点扫描的形状不对：期望", want, "实得", got)
            return 1
        glass = [e for e in eps if e["anchor"] == "glass_above_main"][0]
        if str(glass["first"]) != "2026-10-07 09:00:01.200000":
            print("FAIL 锚点起点取错（应是该类**第一**行的时刻）：", glass["first"])
            return 1
        if abs(glass["span_sec"] - 1.1) > 0.01:
            print("FAIL 锚点持续时长算错：期望 1.1 秒，实得", glass["span_sec"])
            return 1
        if meta["unparsed"] != 1:
            print("FAIL 未解析的行没有如实报数：", meta)
            return 1
        # 扫出来的时刻必须能直接喂进台账（两边格式口径一致，否则定位时长系统性低估）
        probe: dict[str, Any] = {"schema": 1, "updated": "2026-10-09", "rounds": [],
                                 "entries": [{
            "id": "L1", "symptom": "样例", "component": "玻璃层与层级",
            "channel": "online_user", "introduced_version": None, "introduced_note": None,
            "found_version": "1.4.46", "fixed_version": "1.4.46",
            "failed_lines": ["locate"], "why_missed": None, "defense_added": "样例防线",
            "observed_at": glass["first"].isoformat(timespec="seconds"),
            "rootcause_at": None,
            "locate_unknown_reason": "现场仍在复现，根因未定",
            "source": "log:glass_above_main", "evidence": "verified"}]}
        bad_iso = validate(probe, None)
        if bad_iso:
            print("FAIL 锚点时刻喂进台账被判非法（格式口径不一致）：", bad_iso)
            return 1
        probe["entries"][0]["observed_at"] = "上周三下午"
        if not validate(probe, None):
            print("FAIL 非 ISO 的 observed_at 没被抓到")
            return 1
        try:
            scan_log(Path(td2) / "没有这个文件.log", 7)
            print("FAIL 日志文件不存在时静默返回空表（该报错，不许假绿）")
            return 1
        except RuntimeError:
            pass
    ok += 6
    print(f"self-test 通过：干净样本 0 错 + {len(mutations)} 种失守形态全部被拦 + "
          f"历史遗留不硬拦 + 演练隔离不碰真台账 + 锚点扫描形状/时刻/未解析行数全对"
          f"（{ok} 项断言）")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="线上逃逸数与平均定位时长：算数、复盘、硬拦")
    p.add_argument("--report", action="store_true", help="打印两个数字与失守分布")
    p.add_argument("--gate", metavar="VERSION", help="发版链用：校验台账并要求该版本的复盘声明")
    p.add_argument("--check", action="store_true",
                   help="只校验台账条目自洽（不查某一轮的复盘声明；用例用它，免得断言版本号）")
    p.add_argument("--self-test", action="store_true", help="变异自检")
    p.add_argument("--open", dest="symptom", metavar="现象", help="开一条逃逸，盖 observed_at")
    p.add_argument("--rootcause", dest="entry_id", metavar="ID", help="根因写清，盖 rootcause_at")
    p.add_argument("--close", dest="close_id", metavar="ID", help="收口：修复版本 + 失守防线 + 补的落点")
    p.add_argument("--round", dest="round_version", metavar="VERSION", help="写本轮复盘声明")
    p.add_argument("--id", help="配合 --open 指定 id")
    p.add_argument("--prefix", default="N", help="配合 --open 的 id 前缀（N=线上逃逸/S/G）")
    p.add_argument("--channel", default="online_user", help="配合 --open")
    p.add_argument("--component", help="配合 --open/--close 的缺陷种类")
    p.add_argument("--introduced", help="配合 --open：引入版本")
    p.add_argument("--found-version", dest="found_version", help="配合 --open：发现版本")
    p.add_argument("--version", help="配合 --open：发现版本（等价 --found-version）")
    p.add_argument("--fixed-version", dest="fixed_version", help="配合 --close：修复版本")
    p.add_argument("--failed-lines", dest="failed_lines", nargs="*", default=[],
                   help="配合 --close：static/verify/locate/none")
    p.add_argument("--defense", action="append", default=[], help="配合 --close/--round，可重复")
    p.add_argument("--zero-reason", dest="zero_reason", help="配合 --round：本轮 0 逃逸的原因")
    p.add_argument("--escapes", type=int, default=0, help="配合 --round：本轮逃逸条数（0 也要明写）")
    p.add_argument("--why-missed", dest="why_missed", help="配合 --rootcause：为什么三道防线都没拦住")
    p.add_argument("--note", help="配合 --open 的补充说明")
    p.add_argument("--source", help="配合 --open 的出处")
    p.add_argument("--observed-at", dest="observed_at",
                   help="配合 --open：现象首次可观测时刻（ISO），从日志锚点开条时必须传")
    p.add_argument("--from-log", dest="from_log", action="store_true",
                   help="扫描产品日志，列出各类锚点的现场片段（数字二的真实数据源）")
    p.add_argument("--from-diag", dest="from_diag", metavar="ZIP",
                   help="读用户回传的诊断包（glass.json + debug.log），摊开那台机器的现场")
    p.add_argument("--log-path", dest="log_path", help="配合 --from-log，默认取 SF_LOG_PATH 或装机版日志")
    p.add_argument("--days", type=int, default=3, help="配合 --from-log 的回看天数，默认 3")
    p.add_argument("--ledger", help="台账路径；演练时指向临时副本，别把假缺陷写进唯一数据源")
    args = p.parse_args(argv)

    try:
        if args.ledger:
            global LEDGER_PATH
            LEDGER_PATH = Path(args.ledger)
        if args.self_test:
            return self_test()
        if args.from_log:
            path = Path(args.log_path) if args.log_path else default_log_path()
            print(from_log_report(path, args.days))
            return 0
        if args.from_diag:
            print(from_diag_report(Path(args.from_diag)))
            return 0
        if args.symptom:
            return cmd_open(args)
        if args.entry_id:
            return cmd_rootcause(args)
        if args.close_id:
            args.entry_id = args.close_id
            return cmd_close(args)
        if args.round_version:
            args.version = args.round_version
            return cmd_round(args)
        data = load()
        if args.check:
            return print_gate(data, None)
        if args.gate is not None:
            return print_gate(data, args.gate or None)
        print(report(data))
        return 0
    except RuntimeError as exc:
        # 数据源不可用（git 历史读不到、日志文件不存在都走这里）：宁可不出数，
        # 也不给假数。文案必须照抄真实原因 —— 旧文案把"找不到日志"也报成 git 问题，
        # 排障的人会被带去查仓库（2026-10-12 复核 F5 同族）。
        print(f"!! 数据源不可用，拒绝输出可能编出来的数字：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
