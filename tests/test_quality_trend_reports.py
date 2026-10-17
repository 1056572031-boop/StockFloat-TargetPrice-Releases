# -*- coding: utf-8 -*-
"""逃逸台账的**报表层**与**写操作**在测试主进程里跑（覆盖率轮 A 档，2026-10-12）。

为什么要这一份（实测）：`tools/quality_trend.py` 584 条语句只覆盖到 36.6%，未覆盖的 370 条里 ——
  · `report()` 89 条 + `from_diag_report()` 42 条 + `from_log_report()` 23 条 = 154 条
    报表纯逻辑，**一行都没跑过**；
  · `main()` 63 条 + `cmd_open/rootcause/close/round` 80 条只在发版链 3.7 起**子进程**跑，
    而子进程不计入 `.coverage`（AGENTS §6 第 10 条）。
于是"复盘闸门"这条链的真实状态是：它输出的那两个数字（线上逃逸数、平均定位时长）
从写下那天起就没被任何自动化跑通过，只有人在发版时肉眼看一次。报表里一处除零、
一个键名写错、一版漏打，用户看到的趋势数字就变形，而全量回归一直是绿的。

这一档补的是**行为**不是数字：夹具的时刻、版本、渠道全部由测试给定，所以桶计数、
平均/中位/最长、未解析行数、片段分组这些期望值全部在测试里独立算出来（期望值旁边写了
推导过程），改坏代码必须红 —— 不是"跑一遍没炸就算过"。

四条硬约束：
  1. **演练一律打在临时台账上**，真台账 `tools/escapes_ledger.json` 一个字节都不许变 ——
     它是唯一数据源，写进假条目等于把两个趋势数字永久污染（工具自己的 docstring 里
     记着这条真实踩过的坑）。每条走命令行的用例都在 tearDown 里比对真台账字节。
  2. `--ledger` 会把模块级 `LEDGER_PATH` 改指到临时文件，这是**全局状态**，
     用例外必须恢复（否则同一进程里后面的用例读到的"台账"是我造的假账）。
  3. `report()` 默认跑 `git log -p` 推版本日期（本机实测 ≈1 秒）。断言桶内版本数与
     "每 30 天"那一列需要确定的版本→日期表，所以按用例 patch 掉；真 git 路线另有一条
     用例跑一次并在类里缓存，不给全量添 10 秒。
  4. 报表文本是**列对齐**的（`%-14s %2d 条`），断言一律走 `_flat()` 折叠空白后再比 ——
     直接写单空格会把"排版正确"判成失败。

按 AGENTS §6：断言里不写版本号字面量（R1，全部走模块常量）、不读源码做字符串断言（R2）、
不 sleep（R3）、不依赖当下时钟（R4）、不碰私有成员（R5，一切从 `main(argv)` 的命令行面进），
继承 SFCase（R7）并在 setUp 里先跑基类（`track_patch` 依赖 `_sf_patches` 已初始化）。
"""
from __future__ import annotations

import importlib.util
import io
import json
import pathlib
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from _base import SFCase                          # noqa: E402
except ImportError:                                    # pragma: no cover
    from tests._base import SFCase                    # noqa: E402

QT_PATH = ROOT / "tools" / "quality_trend.py"
REAL_LEDGER = ROOT / "tools" / "escapes_ledger.json"


def _load_tool():
    spec = importlib.util.spec_from_file_location("quality_trend_under_test", str(QT_PATH))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


qt = _load_tool()


def _flat(text: str) -> str:
    """折叠连续空白后再比：报表是列对齐的，原样断言空格数会把排版判成失败。"""
    return " ".join(text.split())


# ---- 夹具：版本 → 日期表（全部由测试给定，report() 里的桶计数才能独立算）----
# 每枚版本都写清落在哪个桶，"每版"与"每 30 天"两列的期望值在测试里手算：
#   桶① {1.2.0(07-20), 1.3.0(08-01)} → 2 版 / 13 天
#   桶② {1.4.0(09-01), 1.4.5(09-03), 1.4.15(09-02)} → 3 版 / 3 天
#   桶③ {1.4.16(09-10), 1.4.20(09-11), 1.4.30(09-13)} → 3 版 / 4 天
#   桶④ {1.4.31(09-20), 1.4.99(09-29)} → 2 版 / 10 天
# 四个桶的 (逃逸/版本数/自然日) 组合互不相同 —— 两个桶数字长得一样时，
# 桶边界写错就抓不到了，所以刻意让它们两两可辨。
# N60 / N70 的发现版本故意不在表里 → 走"推不出发现日期"那一支。
VMAP = {
    "1.2.0": ("2026-07-20", "exact"),
    "1.3.0": ("2026-08-01", "exact"),
    "1.4.0": ("2026-09-01", "exact"),
    "1.4.5": ("2026-09-03", "exact"),
    "1.4.15": ("2026-09-02", "msg"),
    "1.4.16": ("2026-09-10", "exact"),
    "1.4.20": ("2026-09-11", "exact"),
    "1.4.30": ("2026-09-13", "exact"),
    "1.4.31": ("2026-09-20", "exact"),
    "1.4.99": ("2026-09-29", "msg"),
}

# 发现/引入版本写成常量：断言里不出现裸版本号（AGENTS §6 第 1 条）
V_BUCKET1 = "1.2.0"
V_BUCKET2 = "1.4.5"
V_BUCKET3 = "1.4.20"
V_BUCKET4 = "1.4.45"
V_UNDATED = "1.4.60"          # 不在 VMAP 里 → 进"推不出发现日期"那一列
V_INTRO_E1 = "1.4.0"
V_INTRO_E2 = "1.3.0"          # 1.3.0 → 1.4.20 之间进过 4 个版本号
V_INTRO_E6 = "1.4.31"
V_FIXED_E1 = "1.4.6"
V_FIXED_E2 = "1.4.21"
V_GLASS_ONLY = "1.4.40"
V_LIAR = "1.4.30"

T_1000 = "2026-10-01 10:00:00+08:00"
T_1030 = "2026-10-01 10:30:00+08:00"      # N01：定位 30 分钟
T_0900 = "2026-10-02 09:00:00+08:00"
T_1030B = "2026-10-02 10:30:00+08:00"     # N02：定位 90 分钟
T_0800 = "2026-10-05 08:00:00+08:00"
T_0900B = "2026-10-05 08:20:00+08:00"     # N60：定位 20 分钟（**刻意不等于平均值的一半之类**，
                                          # 三段时长要 30/90/20 互不相同：均值 46.7、中位 30.0，
                                          # 两个数不相等才能把"中位写成均值"这种改坏判出来
T_ONLY_START = "2026-10-06 09:15:00+08:00"  # N70：只有起点，还差终点

RISK_OPEN = "同类仍有无判据路径"
RISK_DONE = "已收敛"
REASON_NO_FIELD = "没有现场时刻，起点无法追溯"
# 演练专用版本号：真台账里不存在这一版，validate(…, 本号) 只可能被我这条链路写的条目影响
V_REHEARSAL = "9.9.5"


def _entry(eid, channel, found, component, failed_lines, *, observed=None,
           rootcause=None, unknown_reason=None, defense=None, introduced=None,
           fixed=None, symptom="样例现象", risk=None):
    return {
        "id": eid, "symptom": symptom, "component": component, "channel": channel,
        "introduced_version": introduced, "introduced_note": None,
        "found_version": found, "fixed_version": fixed,
        "failed_lines": failed_lines, "why_missed": None, "defense_added": defense,
        "observed_at": observed, "rootcause_at": rootcause,
        "locate_unknown_reason": unknown_reason, "source": "fixture",
        "evidence": "verified", "recurrence_risk": risk,
    }


def fixture_ledger():
    """七条条目把 report() 的分支点齐，且整体能过 validate（--check 必须退 0）。

    线上逃逸（三个进分母的渠道）= N01 / N02 / N05 / N60 / N70 共 5 条：
      桶① N05(1.2.0) · 桶② N01(1.4.5) · 桶③ N02(1.4.20) · 桶④ N60(1.4.60)+N70(1.4.45)
    可算定位时长 = 30 / 90 / 60 分钟三条（平均 60.0，中位 60.0，最长 90.0）
    S77 是"已上线没人报"、G05 是"关卡自己说假话" —— 都不进线上逃逸数。
    """
    entries = [
        _entry("N01", "online_user", V_BUCKET2, "玻璃层与层级", ["verify"],
               observed=T_1000, rootcause=T_1030,
               defense="补玻璃层合成顺序判据", introduced=V_INTRO_E1, fixed=V_FIXED_E1,
               symptom="只剩背景，内容窗看不见"),
        _entry("N02", "second_machine", V_BUCKET3, "行情与分时", ["static", "verify"],
               observed=T_0900, rootcause=T_1030B,
               introduced=V_INTRO_E2, fixed=V_FIXED_E2,
               symptom="iMac 上分时图整段空白", risk=RISK_OPEN),
        _entry("S77", "shipped_unnoticed", V_GLASS_ONLY, "玻璃层与层级", ["locate"],
               unknown_reason=REASON_NO_FIELD, symptom="抬层整段从未执行"),
        _entry("G05", "gate_liar", V_LIAR, "关卡自审", ["none"],
               unknown_reason=REASON_NO_FIELD, symptom="产物核验器五十多个版本恒过"),
        _entry("N05", "online_user", V_BUCKET1, "提醒与日历", ["none"],
               unknown_reason=REASON_NO_FIELD, defense=None, risk=RISK_OPEN,
               symptom="到点没提醒"),
        _entry("N60", "online_user", V_UNDATED, "行情与分时", ["verify"],
               observed=T_0800, rootcause=T_0900B,
               defense="补主备串行预算", introduced=V_INTRO_E6,
               symptom="分时显示重试中很久", risk=RISK_DONE),
        _entry("N70", "installed_field", V_BUCKET4, "提醒与日历", ["none"],
               observed=T_ONLY_START, rootcause=None, defense=None,
               unknown_reason="现场仍在复现，根因未定",
               symptom="装机版第一次启动没弹授权框"),
    ]
    rounds = [{"version": "9.9.%d" % i, "date": "2026-10-%02d" % (10 + i),
               "reviewed": True, "escapes_this_round": 0,
               "defense_added": [], "zero_reason": "本轮为纯测试轮"}
              for i in range(1, 7)]
    return {"schema": 1, "updated": "2026-10-11", "entries": entries,
            "rounds": rounds, "notes": []}


def _bucket_rows(out):
    """【数字一】那张表读成 {桶名: [逃逸, 版本数, 自然日, 每版, 每30天]}。"""
    rows = {}
    for line in out.splitlines():
        for label, _lo, _hi in qt.BUCKETS:
            if line.startswith(label):
                rows[label] = line[len(label):].split()
    return rows


def _line_counts(out):
    got = {}
    for line in out.splitlines():
        if "失守" in line and "次" in line:
            label, tail = line.strip().split("失守")
            got[label.strip()] = int(tail.replace("次", "").strip())
    return got


def _component_counts(out):
    got, order = {}, []
    for line in out.splitlines():
        s = line.strip()
        parts = s.split()
        if len(parts) == 3 and parts[2] == "条":
            got[parts[0]] = int(parts[1])
            order.append(int(parts[1]))
    return got, order


# ---- 诊断包夹具（DiagReport 与 CLI 那条共用，避免在用例里实例化 TestCase）----
GLASS_OK = {
    "version": "1.4.46", "run": "beef01", "native_cocoa": True,
    "glass_present": True, "follow_timer_active": False,
    "real_state": {"glass_on_top": True, "desync_px": 11.5},
    "retop_last": {"glass above main": {"age_sec": 3.2, "suppressed": 2}},
    "anchors": {"window_minutes": 30, "read_bytes": 1234, "truncated": False,
                "unparsed_lines": 0, "note": "锚点是程序检出的时刻",
                "anchors": [{"key": "glass_above_main", "hits": 3,
                             "first_seen": "2026-10-09T09:12:03.400",
                             "last_seen": "2026-10-09T09:22:03.400",
                             "runs": ["beef01"], "first_run": "beef01",
                             "desc": "背景层压在内容之上",
                             "sample": "re-top: glass above main"}]},
}

SAMPLE_LOG = (
    "[2026-10-07 09:00:00.100 a1b2c3] INFO: STARTUP v1.4.45 run=a1b2c3\n"
    "[2026-10-07 09:00:01.200 a1b2c3] WARNING: re-top: glass above main ×3\n"
    "[2026-10-07 09:00:02.300 a1b2c3] WARNING: re-top: glass above main\n"
    "[2026-10-07 09:00:03.400 a1b2c3] WARNING: order probe blind: pair not both on-screen\n"
    "[2026-10-08 10:00:04.500 d4e5f6] ERROR: SWALLOWED sf_glass._force_top: AttributeError\n"
    "这行不是标准格式（traceback 续行）\n"
)


def make_diag_zip(glass=None, log_text=None, holder=None, name="诊断.zip"):
    td = pathlib.Path(tempfile.mkdtemp())
    zp = td / name
    with zipfile.ZipFile(zp, "w") as z:
        if glass is not None:
            z.writestr("glass.json", json.dumps(glass, ensure_ascii=False))
        if log_text is not None:
            z.writestr("debug.log", log_text)
    if holder is not None:
        holder.addCleanup(lambda: [f.unlink() for f in td.glob("*")])
    return zp


def make_log_file(text=None, holder=None):
    td = pathlib.Path(tempfile.mkdtemp())
    p = td / "debug.log"
    p.write_text(SAMPLE_LOG if text is None else text, encoding="utf-8")
    if holder is not None:
        holder.addCleanup(lambda: [f.unlink() for f in td.glob("*")])
    return p


class ReportTwoNumbers(SFCase):
    """report() 的两个数字：桶计数、平均/中位/最长，全部对独立算出的期望值。"""

    def setUp(self):
        super().setUp()
        self.track_patch(mock.patch.object(qt, "version_dates", return_value=dict(VMAP)))
        self.out = qt.report(fixture_ledger())

    def test_口径先声明没有逃逸占比这个分母(self):
        # 这条锁的是口径本身：把 shipped_unnoticed / gate_liar 混进线上逃逸数，
        # 趋势数字会立刻好看，而没有任何地方会报错。
        self.assertIn("只登记已进入已发布产物的缺陷", self.out)
        self.assertIn("「逃逸占比」这一分母", self.out)
        self.assertNotIn("逃逸占比 =", self.out)

    def test_四个桶的逃逸数与两个分母(self):
        rows = _bucket_rows(self.out)
        self.assertEqual(len(rows), len(qt.BUCKETS), "四个桶必须各出一行：%s" % list(rows))
        b1, b2, b3, b4 = (rows[label] for label, _lo, _hi in qt.BUCKETS)
        # 期望值手算（逃逸 / 版本数 / 自然日 / 每版=逃逸÷版本数 / 每 30 天=逃逸×30÷自然日）
        self.assertEqual(b1, ["1", "2", "13", "0.50", "2.31"])
        self.assertEqual(b2, ["1", "3", "3", "0.33", "10.00"])
        self.assertEqual(b3, ["1", "3", "4", "0.33", "7.50"])
        # 两条发现版本均无日期：只计入原始逃逸数，不伪装成有日期的每30天分子。
        self.assertEqual(b4, ["2", "2", "10", "1.00", "0.00"])

    def test_平均中位最长三个数(self):
        # 三段时长 30 / 90 / 20：平均 46.7、中位 30.0、最长 90.0。
        # 平均值与中位数刻意不同 —— 否则把中位写成均值这类改坏判不出来（实测漏过一次）。
        self.assertIn("可计算条目 3 条，平均 46.7 分钟，中位 30.0 分钟，最长 90.0 分钟",
                      self.out)

    def test_只有起点没有终点的条目单独列出(self):
        self.assertIn("已有起点、还差终点", self.out)
        self.assertIn("N70", self.out)

    def test_推不出发现日期的条目如实点名(self):
        # 线上逃逸 5 条里 N60 / N70 的发现版本不在版本表 → 3/5 能推出日期
        self.assertIn("其中 3/5 条能推出发现日期", self.out)
        self.assertIn("N60, N70", self.out)

    def test_已上线没人报与关卡说假话不进线上逃逸数(self):
        self.assertIn("另有 1 条【已上线但用户看不见或没人报】", self.out)
        self.assertIn("1 条【关卡自己会说假话】", self.out)

    def test_防线失守分布按防线计数(self):
        got = _line_counts(self.out)
        # static: N02 · verify: N01,N02,N60 · locate: S77 · none: G05,N05,N70
        self.assertEqual(got["防线一 静态"], 1)
        self.assertEqual(got["防线二 验证"], 3)
        self.assertEqual(got["防线三 定位"], 1)
        self.assertEqual(got["不该指望防线"], 3)

    def test_组件分布与降序排列(self):
        got, order = _component_counts(self.out)
        self.assertEqual(got["玻璃层与层级"], 2)
        self.assertEqual(got["行情与分时"], 2)
        self.assertEqual(got["提醒与日历"], 2)
        self.assertEqual(got["关卡自审"], 1)
        self.assertEqual(order, sorted(order, reverse=True), "组件分布必须按条数降序")

    def test_逃逸里没补防线的条目点名(self):
        # strict 里 defense_added 为空的是 N02 / N05 / N70
        self.assertIn("3 条至今没登记「补上了什么防线」：N02, N05, N70", self.out)

    def test_复发风险按条数降序分组(self):
        flat = _flat(self.out)
        self.assertIn("%s 2 条：N02, N05" % RISK_OPEN, flat)
        self.assertIn("%s 1 条：N60" % RISK_DONE, flat)

    def test_潜伏跨度数版本号而不是时长(self):
        # N02：1.3.0 → 1.4.20 之间进过 {1.3.0,1.4.0,1.4.5,1.4.15,1.4.16} = 5 个版本
        first = [l for l in self.out.splitlines() if "个版本  N" in l][0]
        self.assertIn("5 个版本", first)
        self.assertIn("N02", first)
        # 引入版本写不出来的：S77 / G05 / N05 / N70 = 4 条
        self.assertIn("引入版本写不出来的 4 条", self.out)

    def test_复盘闭环只打最后五条(self):
        self.assertIn("rounds 记录 6 条", self.out)
        tail = self.out.split("【复盘闭环】")[1]
        printed = [l for l in tail.splitlines() if l.strip().startswith("v9.9.")]
        self.assertEqual(len(printed), 5, "rounds 有 6 条却打满 6 行 = 窗口没生效")
        first_round = "9.9.1"
        self.assertNotIn(first_round, "\n".join(printed), "最早那条不该出现在最后五条里")

    def test_没有分母时必须说没有分母(self):
        """把三个时刻全抹掉：这一档必须改口"没有分母"，不许报 0.0 分钟的平均值。

        0.0 与"没有分母"在趋势图上长得一样，含义却相反 —— 这正是本工具立身的那条口径。
        """
        data = fixture_ledger()
        for e in data["entries"]:
            e["observed_at"] = None
            e["rootcause_at"] = None
            e["locate_unknown_reason"] = "抹掉用于验空分母"
        out = qt.report(data)
        self.assertIn("可计算条目 **0** 条", out)
        self.assertIn("没有分母", out)
        self.assertIn("两轮之后这个数才第一次可比", out)
        self.assertNotIn("平均 0.0 分钟", out)

    def test_缺理由的条目进警告而不是静默算作零(self):
        data = fixture_ledger()
        data["entries"][0]["observed_at"] = None
        data["entries"][0]["rootcause_at"] = None
        data["entries"][0]["locate_unknown_reason"] = None
        out = qt.report(data)
        self.assertIn("既没有时刻戳、也没写「为什么记不到」", out)
        self.assertIn("N01", out)


REAL_REPORT = {"text": None}   # 真 git + 真台账只跑一次（git log -p 实测 ≈1 秒）


def real_report_text():
    if REAL_REPORT["text"] is None:
        REAL_REPORT["text"] = qt.report(qt.load())
    return REAL_REPORT["text"]


class ReportAgainstRealLedger(SFCase):
    """真台账 + 真 git 版本表跑一次：这条是给"报表在真实数据上会崩"准备的。"""

    def test_真台账能出完整报表(self):
        out = real_report_text()
        for head in ("【数字一】", "【数字二】", "【防线失守分布】", "【潜伏跨度】",
                     "【复盘闭环】"):
            self.assertIn(head, out, "真数据下报表缺了整节：%s" % head)
        self.assertEqual(len(_bucket_rows(out)), len(qt.BUCKETS))

    def test_真台账的报表里没有异常占位(self):
        out = real_report_text()
        self.assertNotIn("Traceback", out)
        self.assertNotIn("None 条", out)


class LogAnchorReport(SFCase):
    """`--from-log` 的报表：数字二的唯一数据源。扫不出锚点时必须区分"没发生"与"没读到"。"""

    def test_有锚点时给出片段表与可直接抄的开条命令(self):
        out = qt.from_log_report(make_log_file(holder=self), 365)
        self.assertIn("日志锚点扫描", out)
        self.assertIn("起点口径=程序检出时刻", out)
        # 同一 run 内同类锚点归成一个片段，起点=该类**第一**行（毫秒三位）
        self.assertIn("glass_above_main a1b2c3 2 1.1 2026-10-07T09:00:01.200", _flat(out))
        self.assertIn("--observed-at", out)
        self.assertIn('--channel online_user', out)

    def test_分组行里的片段数行数与最长一段(self):
        raw = qt.from_log_report(make_log_file(holder=self), 365)
        out = _flat(raw)
        self.assertIn("glass_above_main 1 个片段 / 2 行 · 最长一段 1.1 秒（run a1b2c3）", out)
        self.assertIn("swallowed 1 个片段 / 1 行", out)
        # 分组行按命中总数降序：两行命中的 glass 必须排在各只命中一行的两类前面
        order = [line.split()[0] for line in raw.splitlines() if "个片段 /" in line]
        self.assertEqual(order[0], "glass_above_main")
        self.assertEqual(len(order), 3)

    def test_未解析行数如实报(self):
        out = qt.from_log_report(make_log_file(holder=self), 365)
        self.assertIn("1 行不是标准行格式", out)
        self.assertIn("「没锚点」不等于「没出事」", out)

    def test_窗口里没有锚点时不许假称没发生过(self):
        only_old = "[2020-01-01 00:00:00.000 a1b2c3] WARNING: re-top: glass above main\n"
        out = qt.from_log_report(make_log_file(only_old, holder=self), 3)
        self.assertIn("这个窗口内没有任何锚点", out)
        self.assertIn("或日志已轮转掉", out)

    def test_空日志也要说明读取范围(self):
        out = qt.from_log_report(make_log_file("", holder=self), 7)
        self.assertIn("这个窗口内没有任何锚点", out)

    def test_日志文件不存在必须炸不许返回空表(self):
        ghost = pathlib.Path(tempfile.mkdtemp()) / "没有这个.log"
        with self.assertRaises(RuntimeError):
            qt.from_log_report(ghost, 7)


class DiagReport(SFCase):
    """`--from-diag`：现场在用户机器上时这是唯一的一手证据，四种包形状各有一条口径。"""

    def test_带现场的包摊开合成顺序锚点与开条命令(self):
        zp = make_diag_zip(glass=GLASS_OK, log_text=SAMPLE_LOG, holder=self)
        out = qt.from_diag_report(zp)
        self.assertIn("产物 v1.4.46 run=beef01", out)
        self.assertIn("玻璃在上=True", out)
        self.assertIn("真实错位=11.5 px", out)
        self.assertIn("距今 3.2 秒，其间压掉 2 条同类", out)
        self.assertIn('--observed-at "2026-10-09T09:12:03"', out)
        self.assertIn("包里的 debug.log 复核", out)

    def test_旧产物没有现场时必须说不等于没发生(self):
        out = qt.from_diag_report(make_diag_zip(holder=self))
        self.assertIn("没有 glass.json", out)
        self.assertIn("不等于", out)
        self.assertIn("升到带 glass.json 的那一版", out)

    def test_合成顺序问不出来时打印产品侧给的说法(self):
        glass = json.loads(json.dumps(GLASS_OK))
        glass["real_state"] = None
        glass["real_state_note"] = "两窗此刻不在同一 Space"
        out = qt.from_diag_report(make_diag_zip(glass=glass, holder=self))
        self.assertIn("此刻合成顺序：两窗此刻不在同一 Space", out)

    def test_锚点扫描失败时打印失败原因而不是空表(self):
        glass = json.loads(json.dumps(GLASS_OK))
        glass["anchors"] = {"error": "日志文件被占用", "anchors": []}
        out = qt.from_diag_report(make_diag_zip(glass=glass, holder=self))
        self.assertIn("锚点扫描失败：日志文件被占用", out)

    def test_零锚点窗口要带读取字节与截断标记(self):
        glass = json.loads(json.dumps(GLASS_OK))
        glass["anchors"] = {"window_minutes": 30, "read_bytes": 998, "truncated": True,
                            "unparsed_lines": 7, "anchors": []}
        out = qt.from_diag_report(make_diag_zip(glass=glass, holder=self))
        self.assertIn("最近 30 分钟没有锚点", out)
        self.assertIn("读了 998 字节", out)
        self.assertIn("truncated=True", out)
        self.assertIn("未解析行 7", out)

    def test_产品侧新增锚点类而工具没跟随时大声警告(self):
        glass = json.loads(json.dumps(GLASS_OK))
        glass["anchors"]["anchors"].append({
            "key": "brand_new_anchor", "hits": 1, "first_seen": "2026-10-09T11:00:00.000",
            "last_seen": "2026-10-09T11:00:00.000", "runs": ["beef01"],
            "first_run": "beef01", "desc": "新一类现场", "sample": "..."})
        out = qt.from_diag_report(make_diag_zip(glass=glass, holder=self))
        self.assertIn("needle 表还没跟上", out)
        self.assertIn("brand_new_anchor", out)

    def test_包不存在时抛错(self):
        ghost = pathlib.Path(tempfile.mkdtemp()) / "无.zip"
        with self.assertRaises(RuntimeError):
            qt.from_diag_report(ghost)


class LedgerLifecycleViaCli(SFCase):
    """开条→盖根因→收口→写复盘声明，整条走命令行，落在临时台账上。

    为什么必须走 `main(argv)` 而不是直接调 `cmd_*`：路径分发（`--close` 把 `close_id`
    挪进 `entry_id`、`--round` 把 `round_version` 挪进 `version`）本身是这段链路最容易
    写错的地方，而那 63 条 `main()` 语句此前一行都没在能记账的地方跑过。
    """

    def setUp(self):
        super().setUp()
        self.tmpdir = pathlib.Path(tempfile.mkdtemp())
        self.ledger = self.tmpdir / "rehearsal.json"
        self.real_bytes = REAL_LEDGER.read_bytes()
        self.ledger.write_bytes(self.real_bytes)
        self._saved_ledger_path = qt.LEDGER_PATH
        self.track_patch(mock.patch.object(qt, "LEDGER_PATH", self.ledger))

    def tearDown(self):
        # --ledger 会把模块级 LEDGER_PATH 改指到临时文件；不恢复等于给同一进程
        # 后面所有用例换了一本假台账（AGENTS §6：自己种下的状态必须在用例体内收干净）
        qt.LEDGER_PATH = self._saved_ledger_path
        self.assertEqual(REAL_LEDGER.read_bytes(), self.real_bytes,
                         "演练把真台账改了 —— 唯一数据源被污染，两个趋势数字从此是假的")
        super().tearDown()

    def _cli(self, argv):
        buf, err = io.StringIO(), io.StringIO()
        with redirect_stdout(buf), redirect_stderr(err):
            rc = qt.main(argv + ["--ledger", str(self.ledger)])
        return rc, buf.getvalue() + err.getvalue()

    def _mine(self, eid):
        hits = [x for x in qt.load(self.ledger)["entries"] if x["id"] == eid]
        self.assertEqual(len(hits), 1, "台账里没有 id=%s" % eid)
        return hits[0]

    def test_开条盖根因收口写声明一路走到底(self):
        rc, out = self._cli(["--open", "分时图整段空白", "--id", "Z91",
                             "--channel", "online_user", "--component", "行情与分时",
                             "--found-version", V_BUCKET3, "--introduced", V_INTRO_E2,
                             "--source", "rehearsal", "--observed-at", T_1000])
        self.assertEqual(rc, 0, out)
        mine = self._mine("Z91")
        self.assertEqual(mine["observed_origin"], "log",
                         "传了 --observed-at 却记成 now —— 定位时长会把敲命令当起点")
        self.assertEqual(mine["observed_at"], T_1000)
        self.assertIsNone(mine["rootcause_at"])

        rc, out = self._cli(["--rootcause", "Z91", "--why-missed", "没有判据"])
        self.assertEqual(rc, 0, out)
        mine = self._mine("Z91")
        self.assertIsNotNone(mine["rootcause_at"], "终点戳没盖上")
        self.assertIsNone(mine["locate_unknown_reason"], "两个戳都齐了还挂着记不到的理由")
        self.assertEqual(mine["why_missed"], "没有判据")
        self.assertIn("定位时长 =", out)

        rc, out = self._cli(["--close", "Z91", "--fixed-version", V_BUCKET3,
                             "--failed-lines", "verify", "--defense", "补分时预算"])
        self.assertEqual(rc, 0, out)
        mine = self._mine("Z91")
        self.assertEqual(mine["failed_lines"], ["verify"])
        self.assertEqual(mine["defense_added"], ["补分时预算"])
        self.assertEqual(mine["fixed_version"], V_BUCKET3)

        rc, out = self._cli(["--round", V_BUCKET3, "--escapes", "1",
                             "--defense", "补分时预算"])
        self.assertEqual(rc, 0, out)
        rec = [r for r in qt.load(self.ledger)["rounds"] if r["version"] == V_BUCKET3]
        self.assertEqual(len(rec), 1)
        self.assertTrue(rec[0]["reviewed"])
        self.assertEqual(rec[0]["escapes_this_round"], 1)

    def test_不传observed_at时起点记成敲命令那一刻(self):
        rc, out = self._cli(["--open", "没有现场时刻的现象", "--id", "Z92",
                             "--channel", "gate_liar", "--found-version", V_BUCKET4,
                             "--source", "rehearsal"])
        self.assertEqual(rc, 0, out)
        self.assertEqual(self._mine("Z92")["observed_origin"], "now")

    def test_自动编号不撞车(self):
        before = [e["id"] for e in qt.load(self.ledger)["entries"]
                  if str(e["id"]).startswith("Z")]
        self.assertEqual(before, [], "真台账里出现了 Z 开头的 id，演练前缀要换")
        rc, out = self._cli(["--open", "第一条自动号", "--prefix", "Z",
                             "--channel", "online_user", "--found-version", V_BUCKET4,
                             "--source", "rehearsal"])
        self.assertEqual(rc, 0, out)
        rc2, out2 = self._cli(["--open", "第二条自动号", "--prefix", "Z",
                               "--channel", "online_user", "--found-version", V_BUCKET4,
                               "--source", "rehearsal"])
        self.assertEqual(rc2, 0, out2)
        ids = [e["id"] for e in qt.load(self.ledger)["entries"]
               if str(e["id"]).startswith("Z")]
        self.assertEqual(ids, ["Z01", "Z02"], "自动编号撞车 = 台账里两条互相覆盖")

    def test_同一轮重复写声明是替换不是追加(self):
        self._cli(["--round", V_UNDATED, "--escapes", "0", "--zero-reason", "纯测试轮"])
        self._cli(["--round", V_UNDATED, "--escapes", "2", "--defense", "补两条判据"])
        rec = [r for r in qt.load(self.ledger)["rounds"] if r["version"] == V_UNDATED]
        self.assertEqual(len(rec), 1, "同一版写出两条声明 = 3.7 闸门读到哪条看运气")
        self.assertEqual(rec[0]["escapes_this_round"], 2)

    def test_id不存在时报错退出而不是静默(self):
        buf = io.StringIO()
        with self.assertRaises(SystemExit) as cm:
            with redirect_stdout(buf):
                qt.main(["--rootcause", "不存在", "--ledger", str(self.ledger)])
        self.assertIn("没有 id", str(cm.exception))

    def test_写过的临时台账能通过自洽校验(self):
        """链路跑完的产物必须仍然过 validate 且能出报表 —— 否则"能写"与"写得对"是两件事。

        用 9.9.5 这个不存在的版本号：真台账里没有它的条目，validate 只可能被我刚写的
        这一条影响，不会被历史条目的口径带偏。
        """
        self.track_patch(mock.patch.object(qt, "version_dates", return_value=dict(VMAP)))
        self._cli(["--open", "跑完整链路", "--id", "Z94", "--channel", "online_user",
                   "--component", "玻璃层与层级", "--found-version", V_REHEARSAL,
                   "--introduced", V_INTRO_E1, "--source", "rehearsal",
                   "--observed-at", T_1000])
        self._cli(["--rootcause", "Z94"])
        self._cli(["--close", "Z94", "--fixed-version", V_REHEARSAL,
                   "--failed-lines", "verify", "--defense", "补一条判据"])
        rc, out = self._cli(["--round", V_REHEARSAL, "--escapes", "1",
                             "--defense", "补一条判据"])
        self.assertEqual(rc, 0, out)
        data = qt.load(self.ledger)
        errs = qt.validate(data, V_REHEARSAL)
        self.assertEqual(errs, [], "走完链路仍被闸门判红：%s" % errs)
        mine = self._mine("Z94")
        self.assertIsNotNone(mine["rootcause_at"])
        text = qt.report(data)
        self.assertIn("Z94", text, "链路写出来的条目连报表都读不回来")


class CliReadOnly(SFCase):
    """`main()` 的只读分支：--check / --gate / --report / --from-log / --from-diag。

    这些是发版链与人工日常真正敲的命令，此前只在子进程里跑过。参数名写错、分发漏一条，
    全量都是绿的 —— 尤其 `--gate` 退错码等于 3.7 那道硬拦变成摆设。
    """

    def setUp(self):
        super().setUp()
        self.tmpdir = pathlib.Path(tempfile.mkdtemp())
        self.ledger = self.tmpdir / "read-only.json"
        self.ledger.write_text(json.dumps(fixture_ledger(), ensure_ascii=False),
                               encoding="utf-8")
        self._saved = qt.LEDGER_PATH
        self.track_patch(mock.patch.object(qt, "LEDGER_PATH", self.ledger))
        self.track_patch(mock.patch.object(qt, "version_dates", return_value=dict(VMAP)))

    def tearDown(self):
        qt.LEDGER_PATH = self._saved
        super().tearDown()

    def _cli(self, argv):
        buf, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(buf), redirect_stderr(err):
                rc = qt.main(argv)
        except SystemExit as exc:               # argparse 对未知参数直接 sys.exit(2)
            rc = exc.code if isinstance(exc.code, int) else 2
        return rc, buf.getvalue() + err.getvalue()

    def test_check_只验自洽不查某一轮(self):
        rc, out = self._cli(["--check"])
        self.assertEqual(rc, 0, out)
        self.assertIn("逃逸复盘闸门通过", out)

    def test_gate_要求某一版的复盘声明(self):
        rc, out = self._cli(["--gate", "9.9.3"])
        self.assertEqual(rc, 0, out)
        rc2, out2 = self._cli(["--gate", "9.9.77"])
        self.assertEqual(rc2, 1, "缺复盘声明却放行 = 3.7 闸门是枚摆设")
        self.assertIn("复盘声明", out2)

    def test_report_打印两个数字(self):
        rc, out = self._cli(["--report"])
        self.assertEqual(rc, 0, out)
        self.assertIn("【数字一】", out)
        self.assertIn("【数字二】", out)

    def test_from_log_走log_path参数而不碰用户日志(self):
        log = make_log_file(holder=self)
        rc, out = self._cli(["--from-log", "--log-path", str(log), "--days", "365"])
        self.assertEqual(rc, 0, out)
        self.assertIn("glass_above_main", out)

    def test_from_log_日志缺失时退2而不是报没有锚点(self):
        ghost = self.tmpdir / "没有.log"
        rc, out = self._cli(["--from-log", "--log-path", str(ghost)])
        self.assertEqual(rc, 2, "拿不到数据源却退 0 —— 又一枚会说假话的闸门")
        self.assertIn("找不到日志", out)

    def test_from_diag_读临时包(self):
        zp = make_diag_zip(glass=GLASS_OK, holder=self)
        rc, out = self._cli(["--from-diag", str(zp)])
        self.assertEqual(rc, 0, out)
        self.assertIn("诊断包现场", out)

    def test_取不到git时拒绝输出数字(self):
        self.track_patch(mock.patch.object(qt, "version_dates",
                                           side_effect=RuntimeError("没有 git 历史")))
        rc, out = self._cli(["--report"])
        self.assertEqual(rc, 2, "git 拿不到还硬出数 = 把编出来的数字当趋势汇报")
        self.assertIn("拒绝输出", out)

    def test_未知参数退2不落默认动作(self):
        rc, _ = self._cli(["--no-such-flag"])
        self.assertEqual(rc, 2, "AGENTS §3.1：链上工具未知参数一律退 2")


class MeasurementIntegrity(SFCase):
    def setUp(self):
        super().setUp()
        self.track_patch(mock.patch.object(qt, "version_dates", return_value=dict(VMAP)))

    def test_same_second_across_endpoints_is_rejected_and_excluded(self):
        data = fixture_ledger()
        data["entries"] = data["entries"][:2]
        first, second = data["entries"]
        first.update(observed_at="2026-10-09T10:00:00+08:00",
                     rootcause_at="2026-10-09T10:30:00+08:00", locate_unknown_reason=None)
        second.update(observed_at="2026-10-09T02:30:00.800+00:00",
                      rootcause_at="2026-10-09T03:30:00+00:00", locate_unknown_reason=None)
        errs = "\n".join(qt.validate(data))
        for entry in data["entries"]:
            self.assertIn(entry["id"] + ": 记账时刻，不是定位时长", errs)
        out = qt.report(data)
        self.assertIn("时刻未知：2 条", out)
        self.assertIn("可计算条目 **0** 条", out)

    def test_equal_endpoints_and_explicit_unknown_never_enter_average(self):
        data = fixture_ledger()
        for entry in data["entries"]:
            entry["locate_unknown_reason"] = "只知登记时刻"
        entry = data["entries"][0]
        entry.update(observed_at=T_1000, rootcause_at=T_1000, locate_unknown_reason=None)
        self.assertIsNone(qt.locate_minutes(entry))
        self.assertIn("起点终点相同", "\n".join(qt.validate(data)))
        self.assertIn("可计算条目 **0** 条", qt.report(data))

    def test_independent_positive_interval_stays_in_mean(self):
        data = fixture_ledger()
        data["entries"] = data["entries"][:1]
        data["entries"][0].update(observed_at="2026-10-09T10:00:00+08:00",
                                  rootcause_at="2026-10-09T10:37:00+08:00",
                                  locate_unknown_reason=None)
        self.assertEqual(qt.bookkeeping_times(data["entries"]), {})
        self.assertIn("平均 37.0 分钟", qt.report(data))
        self.assertEqual(qt.validate(data), [])

    def test_even_sample_median_is_middle_pair_mean(self):
        data = fixture_ledger()
        data["entries"] = data["entries"][:2]
        for day, entry, minutes in zip((8, 9), data["entries"], (10, 30)):
            entry.update(observed_at=f"2026-10-{day:02}T10:00:00+08:00",
                         rootcause_at=f"2026-10-{day:02}T10:{minutes:02}:00+08:00",
                         locate_unknown_reason=None)
        self.assertIn("中位 20.0 分钟", qt.report(data))

    def test_comparable_window_requires_review_coverage_and_dates(self):
        data = fixture_ledger()
        data["comparison_policy"] = {"start_version": V_BUCKET4, "reason": "测试细记边界"}
        rows = qt.comparable_report(data, dict(VMAP))
        self.assertIn("缺复盘版本", "\n".join(rows))
        self.assertIn("不可比", "\n".join(rows))
        data["rounds"] = [{"version": v, "reviewed": True} for v in VMAP]
        data["entries"] = [e for e in data["entries"] if e["found_version"] in VMAP]
        text = "\n".join(qt.comparable_report(data, dict(VMAP)))
        self.assertNotIn("不可比", text)
        self.assertIn("此前仅历史留痕", text)
        self.assertIn("不能据此宣称逃逸下降", text)

    def test_missing_policy_never_implies_comparable_history(self):
        self.assertIn("不得推导改善", qt.report(fixture_ledger()))

    def test_gate_names_bookkeeping_even_when_reason_claims_it_is_unknown(self):
        data = fixture_ledger()
        entry = data["entries"][0]
        entry.update(rootcause_at=entry["observed_at"], locate_unknown_reason="只是登记")
        out = io.StringIO()
        with redirect_stdout(out):
            rc = qt.print_gate(data, None)
        self.assertEqual(rc, 1)
        self.assertIn(entry["id"] + ": 记账时刻，不是定位时长", out.getvalue())

    def test_audited_entry_and_each_answer_cannot_silently_disappear(self):
        clean = json.loads(REAL_LEDGER.read_text(encoding="utf-8"))
        self.assertEqual(qt.validate(clean), [])
        first_id = clean["recurrence_review"]["entry_ids"][0]
        for field in ("recurrence_risk", "recurrence_guard_refs", "recurrence_liar_check",
                      "recurrence_next_action"):
            data = json.loads(json.dumps(clean))
            entry = next(e for e in data["entries"] if e["id"] == first_id)
            entry.pop(field)
            self.assertTrue(qt.validate(data), field + " 被删了却仍通过")
        clean["entries"] = [e for e in clean["entries"] if e["id"] != first_id]
        self.assertIn("已审查条目丢失：" + first_id, "\n".join(qt.validate(clean)))

    def test_all_reviewed_ids_have_canonical_risk_and_timestamp_audit_keeps_old_values(self):
        data = json.loads(REAL_LEDGER.read_text(encoding="utf-8"))
        reviewed = data["recurrence_review"]
        self.assertEqual(reviewed["previously_rated"] + reviewed["newly_rated"], reviewed["total"])
        by_id = {e["id"]: e for e in data["entries"]}
        for eid in reviewed["entry_ids"]:
            entry = by_id[eid]
            self.assertIn(entry["recurrence_risk"], ("高", "中", "低"))
            self.assertTrue(entry["recurrence_liar_check"])
            self.assertTrue(entry["recurrence_next_action"])
            for ref in entry["recurrence_guard_refs"]:
                self.assertTrue((ROOT / ref).is_file(), ref)
            if entry.get("timestamp_audit"):
                self.assertTrue(entry["timestamp_audit"]["original_observed_at"])
                if entry.get("locate_unknown_reason"):
                    self.assertIsNone(qt.locate_minutes(entry))

    def test_repeated_rootcause_refuses_without_writing(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "ledger.json"
            path.write_text(json.dumps(fixture_ledger()), encoding="utf-8")
            self.track_patch(mock.patch.object(qt, "LEDGER_PATH", path))
            before = path.read_bytes()
            entry = fixture_ledger()["entries"][0]
            with redirect_stdout(io.StringIO()):
                rc = qt.main(["--rootcause", entry["id"]])
            self.assertEqual(rc, 1)
            self.assertEqual(path.read_bytes(), before)

    def test_invalid_comparison_start_refuses_rates(self):
        data = fixture_ledger()
        data["comparison_policy"] = {"start_version": "invalid", "reason": "损坏配置"}
        self.assertEqual(qt.comparable_report(data, dict(VMAP)),
                         ["【可比区间】起始版本无效，拒绝输出可比率。"])

    def test_history_only_entry_does_not_invent_an_empty_comparison_window(self):
        data = fixture_ledger()
        data["comparison_policy"] = {"start_version": V_BUCKET1, "reason": "测试窗口"}
        data["entries"] = [dict(data["entries"][0], channel="gate_liar")]
        text = "\n".join(qt.comparable_report(data, {}))
        self.assertIn("窗口 | 线上逃逸", text)
        self.assertFalse(any(line.startswith("v") and " | " in line for line in text.splitlines()))
        self.assertNotIn("缺版本日期条目", text)
        self.assertIn("不能据此宣称逃逸下降", text)

    def test_gate_names_archived_bookkeeping_without_reintroducing_duration(self):
        data = fixture_ledger()
        entry = data["entries"][0]
        entry.update(observed_at=None, rootcause_at=None,
                     locate_unknown_reason="只有记账时刻，已归档",
                     timestamp_audit={"original_observed_at": T_1000,
                                      "original_rootcause_at": T_1030})
        with redirect_stdout(io.StringIO()) as output:
            rc = qt.print_gate(data, None)
        self.assertEqual(rc, 0, output.getvalue())
        self.assertIn("旧值已归档，平均值剔除）：" + entry["id"], output.getvalue())
        self.assertIsNone(qt.locate_minutes(entry))

    def test_estimated_start_is_not_an_exact_duration(self):
        entry = fixture_ledger()["entries"][0]
        entry["observed_origin"] = "conversation（分钟级估计）"
        self.assertIsNone(qt.locate_minutes(entry))

    def test_unknown_version_date_is_excluded_from_monthly_numerator(self):
        data = fixture_ledger()
        original_rows = _bucket_rows(qt.report(data))
        extra = dict(data["entries"][0], id="UNDATE", found_version=V_UNDATED)
        data["entries"].append(extra)
        changed_rows = _bucket_rows(qt.report(data))
        label = qt.BUCKETS[-1][0]
        self.assertEqual(int(changed_rows[label][0]), int(original_rows[label][0]) + 1)
        self.assertEqual(changed_rows[label][-1], original_rows[label][-1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
