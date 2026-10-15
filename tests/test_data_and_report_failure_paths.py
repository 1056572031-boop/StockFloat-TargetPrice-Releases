# -*- coding: utf-8 -*-
"""数据源与取证渲染的**失败形状**（覆盖率轮 C/D 档，2026-10-12）。

这一档补的不是"再多几个用例"，而是三道防线里第 2、3 道各自最安静的那一类失效：

  · 行情解析（`sf_quote`）：接口改形状时抛的到底是哪种错、北交所/港股/美股的 secid
    拼错会让备源**恒返回空且不报错**、搜索缓存 TTL 过期时留在表里就是拿旧结果冒充新鲜。
  · 崩溃报告渲染（`sf_crashreport`）：这是"用户把包发过来，最关键那件东西在里面没有"
    那一族。`extract_crash_key_sections` 的契约写得很死 —— **绝不向调用方抛异常**，
    因为它外面那层 try 会把整段 `crash_reports` 一起带走（v1.4.18 走查 #2 的原话）。
    渲染体里 `int(base)` 撞 null、`images[i].get` 撞非 dict —— 每一条都得退回纯文本，
    而不是把其它正常报告一起删掉。
  · 临时目录旧副本清扫（`sf_temp_sweep`）：v1.4.5 那枚 SIGSEGV 的唯一解药。
    它的边界（只扫四个根、只认两代 bundle id、跳过在跑的副本、一律改名不删）
    靠的是"出错时也照规矩来"：ps 跑不动就当没有、plist 坏了版本给 "?"、
    日志回调自己炸了不能把清扫带下去。

判据一律走可观测输出：解析结果的字段、渲染出来的文本、改名后的目录名、被调用了几次。
不读源码做字符串断言（AGENTS §6 第 2 条），不 sleep（第 3 条），交易时段走 `fixed_clock`
（第 4 条），不碰私有成员（第 5 条：`_secid` 这类被检对象在文件顶部一次性绑定并写明理由）。
"""
from __future__ import annotations

import json
import os
import pathlib
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import sf_constants
import sf_crashreport as cr
import sf_quote as q
import sf_temp_sweep as sw
from tests._base import SFCase, fixed_clock

PLIST_VERSION = "版本-under-test"      # 不是版本号形状，免得用例被判成"断言版本号"（§6 第 1 条）
_secid = q._secid                            # discipline-ok(R5): 被测对象本身就是私有解析函数
_cache_get = q._search_cache_get             # discipline-ok(R5): 同上（搜索缓存的读口）
quote_primary = q._fetch_quote_primary       # discipline-ok(R5): 同上（主源解析层）
minute_primary = q._fetch_minute_primary     # discipline-ok(R5): 同上（分时主源解析）
clip = cr._clip                              # discipline-ok(R5): 截断规则本身就是被测对象
max_frames = cr._MAX_JSON_FRAMES             # discipline-ok(R5): 同上（堆栈帧数上限）


def tencent_payload(name="贵州茅台", code="sh600519", price="12.50", pre_close="11.00",
                    change="", pct=""):
    """腾讯行情串的原始形状：`v_x="f0~f1~..."`，解析层读的下标是
    f[1]名称 / f[2]代码 / f[3]现价 / f[4]昨收 / f[31]涨跌额 / f[32]涨跌幅。
    前 33 个位置里不关心的都填空串 —— 扩展字段本来就得容错。"""
    f = [""] * 34
    f[1], f[2], f[3], f[4], f[31], f[32] = name, code, price, pre_close, change, pct
    return 'v_sh600519="%s";' % "~".join(f)


def minute_payload(date_str, rows, code="sh600519"):
    return json.dumps({"data": {code: {"data": {"date": date_str, "data": rows}}}})


class SecidMapping(SFCase):
    """东财 secid 的市场段位：拼错不报错，只是备源永远取不到数据。"""

    def test_两字前缀按市场段位映射(self):
        self.assertEqual(_secid("sh600519"), "1.600519")
        self.assertEqual(_secid("sz000001"), "0.000001")
        self.assertEqual(_secid("hk00700"), "116.00700",
                         "港股穿过所有分支落到裸串兜底 = 备源恒空（v1.2.71 那一幕）")

    def test_北交所短码右侧补零(self):
        self.assertEqual(_secid("bj8307"), "0.830700",
                         "东财要 6 位数字，4 位老代码不补零就恒返回空")
        self.assertEqual(_secid("bj830700"), "0.830700")

    def test_九二开头的裸码走北交所段位(self):
        self.assertEqual(_secid("920001"), "0.920001")

    def test_裸代码按首位推断市场(self):
        self.assertEqual(_secid("600519"), "1.600519")
        self.assertEqual(_secid("000001"), "0.000001")
        self.assertEqual(_secid("300750"), "0.300750")
        self.assertEqual(_secid("900001"), "1.900001")

    def test_带空格与大写必须先归一(self):
        """粘贴进自选的那串代码带空格时，旧实现原样回传 " sh600519 " ——
        东财拿到带空格的 secid 恒返回空，而且一句报错都没有。"""
        self.assertEqual(_secid(" sh600519 "), "1.600519")
        self.assertEqual(_secid("SH600519"), "1.600519")

    def test_空值不炸(self):
        for bad in ("", None, "   "):
            self.assertIsInstance(_secid(bad), str, "%r 不该让 secid 抛异常" % (bad,))


class SearchCacheTtl(SFCase):
    """搜索缓存：过期条目必须当"没有"，而不是把三年前的结果当新鲜的返回。"""

    def setUp(self):
        super().setUp()
        from collections import OrderedDict
        self.table = OrderedDict()
        self.track_patch(mock.patch.object(q, "_TEST_ENV", False))
        self.track_patch(mock.patch.object(q, "_SEARCH_CACHE", self.table))

    def test_新鲜条目返回结果并把热度提到末尾(self):
        import time as _t
        self.table["茅台"] = (_t.monotonic(), [("sh600519", "贵州茅台")])
        self.table["先放的一条"] = (_t.monotonic(), [])
        got = _cache_get("茅台")
        self.assertEqual(got, [("sh600519", "贵州茅台")])
        self.assertEqual(list(self.table)[-1], "茅台",
                         "命中后没把热度提到末尾 = 表满时淘汰的正是刚用过的那条")

    def test_过期条目返回空并顺手清掉(self):
        import time as _t
        stale_at = _t.monotonic() - q.SEARCH_CACHE_TTL - 10
        self.table["茅台"] = (stale_at, [("sh600519", "贵州茅台")])
        self.assertIsNone(_cache_get("茅台"), "过期还给结果 = 拿旧搜索结果冒充新鲜")
        self.assertNotIn("茅台", self.table, "过期条目该被清掉，否则表只增不减")

    def test_表里没有这个词时给空而不是KeyError(self):
        self.assertIsNone(_cache_get("没见过的词"))

    def test_测试环境里缓存整体停用(self):
        self.track_patch(mock.patch.object(q, "_TEST_ENV", True))
        self.table["茅台"] = (__import__("time").monotonic(), [("x", "y")])
        self.assertIsNone(_cache_get("茅台"),
                          "用例进程里读到别的用例写的缓存 = 全量与单跑结果不同")


class QuotePayloadGuards(SFCase):
    """主源解析：字段缺/坏/非有限都必须抛 QuoteDataError，而不是把半截数据贴上屏。"""

    def _fetch(self, raw, code="sh600519"):
        """直接打解析层：`fetch_quote` 会把格式错误吞掉再切新浪备源，
        那层的行为由它自己的用例管；这里要验的是"哪种形状该抛哪种错"。"""
        self.track_patch(mock.patch.object(q, "http_get", lambda *a, **k: raw))
        self.track_patch(mock.patch.object(q, "log_line", lambda *a, **k: None))
        return quote_primary(code)

    def test_响应体没有等号引号时判为格式变更(self):
        with self.assertRaises(q.QuoteDataError):
            self._fetch("<html>502 Bad Gateway</html>")

    def test_字段太少或名称代码为空时判为格式变更(self):
        with self.assertRaises(q.QuoteDataError):
            self._fetch('v_sh600519="贵州茅台~600519~12.3";')       # 只有 3 段
        with self.assertRaises(q.QuoteDataError):
            self._fetch(tencent_payload(name=""))                    # 名称为空
        with self.assertRaises(q.QuoteDataError):
            self._fetch(tencent_payload(code=""))                    # 代码为空

    def test_现价昨收为空时判为缺关键字段(self):
        with self.assertRaises(q.QuoteDataError):
            self._fetch(tencent_payload(price=""))
        with self.assertRaises(q.QuoteDataError):
            self._fetch(tencent_payload(pre_close=""))

    def test_nan_与_infinity_都算坏数值(self):
        """float('nan') 能解析成功但会一路传到渲染层 —— 界面显示 nan 就是这条漏过去的。"""
        for bad in ("nan", "inf", "-inf"):
            with self.subTest(bad=bad):
                with self.assertRaises(q.QuoteDataError):
                    self._fetch(tencent_payload(price=bad))

    def test_涨跌额与价差矛盾时记一条WARNING而不是改数(self):
        seen = []
        # 现价-昨收 = 1.50，而接口给的涨跌额是 3.00 —— 两码事对不上
        self.track_patch(mock.patch.object(q, "http_get", lambda *a, **k: tencent_payload(
            price="12.50", pre_close="11.00", change="3.00", pct="27.27")))
        self.track_patch(mock.patch.object(q, "log_line",
                                           lambda *a, **k: seen.append(a)))
        out = quote_primary("sh600519")
        self.assertEqual(out["price"], "12.50", "解析层原样给字段，归一在出口做")
        self.assertTrue(any("semantic warn" in str(x) for x in seen),
                        "字段语义扰动不留痕 = 接口改版后没人知道数据已经不对")


class MinuteDateBranches(SFCase):
    """分时主源：日期标注决定裁剪口径，裁错会把走势图切得七零八落。"""

    TRADING = (2026, 2, 18, 10, 30)          # 周三盘中（fixed_clock 注入，不依赖真时钟）
    CLOSED = (2026, 2, 18, 21, 30)           # 同一天的盘后
    PREVIOUS_DAY = "20260217"

    def _rows(self, n=6):
        """腾讯分时的行形状：`HHMM 价 累计量 累计额`。少于 2 段或时刻不足 4 位都算脏行。"""
        return ["0930 12.00 100 1200", "0931 12.01 120 1441", "0932 12.02 130 1562",
                "0933 12.03 140 1684", "0934 12.04 150 1806", "0935 12.05 160 1928"][:n]

    def _fetch(self, date_str, rows, clock, trading, code="sh600519"):
        """`fixed_clock` 给"现在几点"，`_is_trading_time` 显式给"现在算不算盘中" ——
        分支判的是后者，让它去赌节假日表等于用例随日历变红（AGENTS §6 第 4 条）。"""
        raw = minute_payload(date_str, rows, code)
        self.track_patch(mock.patch.object(q, "http_get", lambda *a, **k: raw))
        self.track_patch(mock.patch.object(q, "log_line", lambda *a, **k: None))
        self.track_patch(mock.patch.object(q, "_is_trading_time", lambda *a: trading))
        with fixed_clock(__import__("datetime").datetime(*clock)):
            return minute_primary(code)

    def test_当日数据盘中裁到当前时刻(self):
        import datetime
        today = datetime.datetime(*self.TRADING).strftime("%Y%m%d")
        rows = ["0930 12.00 100 1200", "0931 12.01 120 1441",
                "1029 12.02 130 1562", "1030 12.03 140 1684", "1400 12.04 150 1806"]
        out = self._fetch(today, rows, self.TRADING, trading=True)
        self.assertEqual([str(pt[0]) for pt in out],
                         ["0930", "0931", "1029", "1030"],
                         "裁到当前时刻失效 = 把还没发生的分钟（14:00）画上线")

    def test_交易时段拿到非当日数据整段丢弃(self):
        out = self._fetch(self.PREVIOUS_DAY, self._rows(), self.TRADING, trading=True)
        self.assertEqual(out, [], "盘中接受昨天的分时 = 图上出现一段拼接的假走势")

    def test_休市时接受最近交易日完整分时(self):
        out = self._fetch(self.PREVIOUS_DAY, self._rows(), self.CLOSED, trading=False)
        self.assertEqual(len(out), len(self._rows()),
                         "休市裁剪到当前时刻 = 晚上看盘只剩开盘几分钟")

    def test_没有日期标注时按时段推测(self):
        out = self._fetch("", self._rows(), self.CLOSED, trading=False)
        self.assertEqual(len(out), len(self._rows()))
        # 同一条无日期标注的报文，盘中时段就该裁到当前时刻
        clipped = self._fetch("", ["0930 12.00 1 1", "1400 12.04 1 1"],
                              self.TRADING, trading=True)
        self.assertEqual([str(pt[0]) for pt in clipped], ["0930"],
                         "无日期 + 盘中却不裁剪 = 把下午的分钟画进上午的图")

    def test_形状不对的分钟行被跳过而不是抛错(self):
        rows = self._rows() + ["乱七八糟", "", "093", "0936 不是数 1 1"]
        out = self._fetch(self.PREVIOUS_DAY, rows, self.CLOSED, trading=False)
        self.assertEqual(len(out), len(self._rows()),
                         "脏行把后面的好行也带没了 = 一处脏数据毁掉整条分时")
        self.assertTrue(all(isinstance(pt, tuple) and isinstance(pt[1], float)
                            for pt in out), "点形状不对 = 渲染层画到一半崩")

    def test_接口整体形状不认识时给空列表(self):
        self.track_patch(mock.patch.object(q, "http_get",
                                           lambda *a, **k: json.dumps({"code": 0})))
        self.track_patch(mock.patch.object(q, "log_line", lambda *a, **k: None))
        self.track_patch(mock.patch.object(q, "_is_trading_time", lambda *a: False))
        with fixed_clock(__import__("datetime").datetime(*self.CLOSED)):
            self.assertEqual(minute_primary("sh600519"), [])


class CrashReportRendering(SFCase):
    """崩溃报告抽取：任何解析失败都要退回纯文本或空串，绝不带走整段证据。"""

    def _ips(self, header=None, body=None):
        docs = []
        if header is not None:
            docs.append(json.dumps(header))
        if body is not None:
            docs.append(json.dumps(body))
        return "\n".join(docs)

    def test_两份文档的ips能渲染出身份行与堆栈(self):
        header = {"app_name": "Skyrocket", "bug_type": "309",
                  "os_version": "macOS 27.0.1", "timestamp": "2026-10-12 10:00:00"}
        body = {"exception": {"type": "EXC_BAD_ACCESS", "signal": "SIGSEGV",
                              "codes": "0x1"},
                "termination": {"reason": "KERN_INVALID_ADDRESS at 0x1"},
                "usedImages": [{"name": "/usr/lib/libSystem.B.dylib"},
                               {"name": "/Applications/Skyrocket.app/x"}],
                "threads": [{"name": "com.apple.main-thread", "crashed": True,
                             "frames": [{"symbol": "objc_opt_self", "imageIndex": 1},
                                        {"imageIndex": 0},
                                        "不是 dict 的一帧",
                                        {"imageIndex": 99}]},
                            {"frames": [{"symbol": "worker 的帧不该出现"}]}]}
        out = cr.extract_crash_key_sections(self._ips(header, body))
        self.assertIn("Skyrocket", out)
        self.assertIn("EXC_BAD_ACCESS", out)
        self.assertIn("objc_opt_self", out)
        self.assertIn("libSystem", out, "symbol 缺失时要按 imageIndex 映射到镜像名")
        self.assertIn("?", out, "越界的 imageIndex 要给 ?，不能抛 IndexError")
        self.assertNotIn("worker", out, "只渲染崩溃线程；把别的线程灌进来会挤掉关键那一段")
        self.assertIn("Key Images", out)

    def test_崩溃线程没有frames列表时说明没有而不是整段消失(self):
        body = {"threads": [{"crashed": True, "frames": "不是列表"}]}
        out = cr.extract_crash_key_sections(self._ips({"app_name": "Skyrocket"}, body))
        self.assertIn("(no frames)", out,
                      "解析不出来就一句都不给 —— 事后问『那次崩在哪』会无从回答")

    def test_帧数超过上限时给出还剩多少(self):
        frames = [{"symbol": "f%d" % i} for i in range(max_frames + 5)]
        body = {"threads": [{"crashed": True, "frames": frames}]}
        out = cr.extract_crash_key_sections(self._ips(None, body))
        self.assertIn("more frames", out, "截了却不说明还剩几帧 = 读的人以为堆栈就这么深")

    def test_exception是字符串时也要给出(self):
        out = cr.extract_crash_key_sections(
            self._ips({"app_name": "Skyrocket"}, {"exception": "EXC_CRASH"}))
        self.assertIn("EXC_CRASH", out)

    def test_没标出崩溃线程时退回线程0(self):
        body = {"threads": [{"frames": [{"symbol": "main"}]},
                            {"frames": [{"symbol": "worker"}]}]}
        out = cr.extract_crash_key_sections(self._ips(None, body))
        self.assertIn("main", out, "报告没写 crashed 就一条堆栈都不给 = 白导一次")

    def test_半截JSON与垃圾文本都不许抛(self):
        for junk in ("{", "{not json}", "[]", '{"threads": [{"frames": [{"imageIndex": null}]}]}',
                     '{"base": null, "usedImages": [null]}', '{"threads": null}'):
            with self.subTest(text=junk[:24]):
                try:
                    res = cr.extract_crash_key_sections(junk)
                except Exception as exc:                       # noqa: BLE001
                    self.fail("渲染抛异常了（%r）—— 外层 try 会把整段 crash_reports 一起吞掉"
                              % (exc,))
                self.assertIsInstance(res, str)

    def test_纯文本崩溃报告仍能抽出线程块(self):
        text = ("Incident Identifier: ABC\n"
                "Thread 0::  Dispatch queue: com.apple.main-thread\n"
                "0  Skyrocket  0x100 main + 12\n"
                "\n"
                "Thread 5 Crashed:\n"
                "0  libsystem  0x200 abort\n"
                "\n"
                "Binary Images:\n"
                "0x100 - 0x200 Skyrocket\n")
        out = cr.extract_crash_key_sections(text)
        self.assertIn("Thread N Crashed", out)
        self.assertIn("abort", out)
        self.assertNotIn("Binary Images:", out, "镜像清单整段灌进诊断包会挤掉日志轮转")

    MARK = "\n...[truncated]"

    def test_按字节截断不切断多字节字符且成品不超上限(self):
        """两件事都要守：不能把 UTF-8 字符切半（诊断包会整份读不出来），
        以及成品（含标记）≤ limit_bytes —— extract_crash_key_sections 的 docstring
        就是按这个承诺做整包体积预算的（2026-10-12 复核修复：旧实现超 15 字节）。"""
        cjk = "崩" * 500                       # 每个 3 字节：正好会切进字符中间
        for limit in (7, 20, 200, 401):
            out = clip(cjk, limit)
            self.assertLessEqual(len(out.encode("utf-8")), limit, "字节上限没守住")
            out.encode("utf-8").decode("utf-8")    # 切在多字节中间会在这一行炸
            if limit > len(self.MARK.encode("utf-8")):
                # 上限连标记都装不下时返回空串（有界优先）；装得下就必须带标记
                self.assertTrue(out.endswith("...[truncated]"),
                                "截了却不说明 = 读的人以为报告就这么长")
            else:
                self.assertEqual(out, "", "装不下标记的极限上限应返回空串")
        self.assertEqual(clip("abc", 0), "", "上限 0 该给空串而不是整段")
        self.assertEqual(clip("abc", -5), "")
        self.assertEqual(clip("abc", 99), "abc", "没超上限不该动原文")

    def test_极限上限只装得下标记时也保有界(self):
        """limit 比标记还小的极端输入：不许回绕成超长成品，也不许抛错。"""
        out = clip("崩" * 50, 5)
        self.assertLessEqual(len(out.encode("utf-8")), 5)
        out2 = clip("abcdef", 14)               # 正好等于标记长度
        self.assertLessEqual(len(out2.encode("utf-8")), 14)

    def test_七天窗口内的报告才进包(self):
        now = 1_800_000_000.0
        fresh = "%s-2026-10-12.ips" % sf_constants.APP_DISPLAY_NAME
        old = "%s-2026-09-01.ips" % sf_constants.APP_DISPLAY_NAME
        picked = cr.select_crash_reports([(fresh, now), (old, now - 30 * 86400)],
                                         now=now)
        self.assertEqual([n for n, _m in picked], [fresh],
                         "30 天前那份不该进包 —— 7 天窗口是诊断包的口径")

    def test_窗口内一个都没有时回退取最新的(self):
        """全在 7 天外也不能整段空手：用户问"我那天崩过一次"时至少要给出最近那份。"""
        now = 1_800_000_000.0
        a = "%s-a.ips" % sf_constants.APP_DISPLAY_NAME
        b = "%s-b.crash" % sf_constants.APP_DISPLAY_NAME
        picked = cr.select_crash_reports([(a, now - 40 * 86400), (b, now - 20 * 86400)],
                                         now=now)
        self.assertEqual([n for n, _m in picked], [b, a], "回退档也要按新→旧")

    def test_别人的崩溃报告与坏mtime都不进(self):
        now = 1_800_000_000.0
        entries = [("Safari-2026-10-12.ips", now),                   # 不是本程序
                   ("%s-x.ips" % sf_constants.APP_DISPLAY_NAME, "昨天"),   # mtime 不是数
                   ("%s-y.txt" % sf_constants.APP_DISPLAY_NAME, now)]      # 后缀不对
        self.assertEqual(cr.select_crash_reports(entries, now=now), [],
                         "把第三方崩溃报告打进用户的诊断包 = 泄隐私又挤掉日志轮转")


class TempSweepDefensive(SFCase):
    """临时副本清扫的兜底层：出错时也必须照规矩来，且一句假话都不说。"""

    def setUp(self):
        super().setUp()
        self.tmp = pathlib.Path(tempfile.mkdtemp())

    def _app(self, name, bundle_id, version=None, broken_plist=False):
        """造一份"还能被 LaunchServices 拉起来"的副本：`is_launchable` 要 Contents/MacOS
        下真有文件 —— 没有它，清扫本来就该放过（那只是一具空壳，不是隐患）。"""
        app = self.tmp / name
        (app / "Contents" / "MacOS").mkdir(parents=True)
        (app / "Contents" / "MacOS" / "Skyrocket").write_text("#!/bin/sh\nexit 0\n")
        plist = app / "Contents" / "Info.plist"
        if broken_plist:
            plist.write_bytes(b"\x00\x01not a plist")
            return app
        payload = {"CFBundleIdentifier": bundle_id}
        if version is not None:
            payload["CFBundleShortVersionString"] = version
        plist.write_bytes(plistlib.dumps(payload))
        return app

    def test_坏plist与缺文件都返回问号而不是抛(self):
        self.assertEqual(sw.version_of(str(self._app("a.app", "x", broken_plist=True))), "?")
        self.assertEqual(sw.version_of(str(self.tmp / "不存在.app")), "?")
        good = self._app("b.app", sf_constants.BUNDLE_ID, version=PLIST_VERSION)
        self.assertEqual(sw.version_of(str(good)), PLIST_VERSION,
                         "plist 里的版本要原样读出来，别在这里断言真实版本号")

    def test_四个根的候选去重且只留存在的目录(self):
        roots = sw.temp_roots()
        self.assertEqual(len(roots), len(set(roots)), "根重复会让同一副本命中两次并写假故障日志")
        for r in roots:
            self.assertTrue(os.path.isdir(r), "%r 不是目录却进了扫描根" % r)
        self.assertTrue(any("folders" in r or "T" in r for r in roots),
                        "$TMPDIR 那一路没进根 = 装机版实测的嵌套副本会漏掉")

    def test_ps跑不动就当没有正在运行的副本(self):
        boom = mock.Mock(side_effect=subprocess.SubprocessError("ps 不可用"))
        self.track_patch(mock.patch.object(sw.subprocess, "run", boom))
        paths = sw.running_bundle_paths()
        self.assertIsInstance(paths, set, "ps 失败不该把整次清扫带停")

    def test_ps输出里的路径会被归一(self):
        out = ("/Applications/Skyrocket.app/Contents/MacOS/Skyrocket\n"
               "/private/var/folders/xx/T/实时股价-目标价版.app/Contents/MacOS/Skyrocket\n")
        got = sw.bundle_paths_from_ps(out)
        self.assertTrue(any(g.endswith(".app") for g in got), "应还原出 .app 目录本身")
        self.assertTrue(all("MacOS" not in g for g in got))

    def test_扫描遇到权限错误时如实记一行并返回零(self):
        self.track_patch(mock.patch.object(sw, "find_stale_apps",
                                           side_effect=OSError("Operation not permitted")))
        notes = []
        renamed = sw.sweep_temporary_copies([str(self.tmp)], notes.append)
        self.assertEqual(renamed, 0)
        self.assertTrue(any("scan failed" in n for n in notes),
                        "扫不动却静默返回 0 = 下一轮没人知道清扫没跑")

    def test_日志回调自己炸不能把清扫带下去(self):
        app = self._app("实时股价-目标价版.app", sf_constants.LEGACY_BUNDLE_IDS[0],
                        version="1.3.97")

        def angry(_msg):
            raise RuntimeError("日志句柄已关闭")

        changed = sw.sweep_temporary_copies([str(self.tmp)], angry)
        self.assertEqual(changed, 1, "清扫本身没跑成")
        renamed = self.tmp / (app.name + ".stale")
        self.assertTrue(renamed.exists(),
                        "旧副本必须被**改名**而不是删除（去掉后缀还能还原）")
        self.assertFalse(app.exists(), "原路径还在就等于 LaunchServices 还能把它拉起来")

    def test_正在运行的副本一个都不碰(self):
        app = self._app("实时股价-目标价版.app", sf_constants.BUNDLE_ID, version="1.3.97")
        running = {os.path.realpath(str(app))}
        self.track_patch(mock.patch.object(sw, "running_bundle_paths",
                                           lambda: running))
        notes = []
        changed = sw.sweep_temporary_copies([str(self.tmp)], notes.append)
        self.assertEqual(changed, 0)
        self.assertTrue(app.exists(), "在用的副本被改名 = 用户正在跑的程序突然没了")

    def test_第三方app不在射程内(self):
        other = self._app("SomeOther.app", "com.example.thirdparty", version="9.9")
        notes = []
        changed = sw.sweep_temporary_copies([str(self.tmp)], notes.append)
        self.assertEqual(changed, 0, "放宽到第三方 app 是 AGENTS §3.1 明令禁止的")
        self.assertTrue(other.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
