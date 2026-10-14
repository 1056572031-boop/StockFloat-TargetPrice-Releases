#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""现场取证链路的不变量（2026-10-09 起）：锚点扫得对、诊断包带得走、工具读得回来。

为什么单独守这一条：玻璃层那一类 8 条线上逃逸的共同根子是**现场只在用户那台机器上**
（台账第八节第一条、D-04/D-31 至今写着"未确认，需现场取证"）。现在把「程序检出的时刻」
并进了诊断包（glass.json），这条链上任一环说谎，定位时长就永远是"记不到"，
更坏的是会被读成"没发生过"。刻意只守四件事，不铺开：
  1) `recent_log_anchors` 的形状：类别、次数、首次/末次、run、未解析行数、时间窗、只读尾部；
  2) 读不到日志时必须**如实带 error**，不许返回空表冒充"没有锚点"；
  3) `glass_diagnostics` 在离屏进程里不碰 AppKit（碰了是不可捕获的段错误，不是异常），
     且仍要带回锚点与"为什么这一项没有值"；
  4) 工具侧 needle 与产品侧 `GLASS_ANCHORS` **同键同 needle** —— 漂移的后果正是"扫不到却报没有"。
"""
import datetime
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import sf_logging  # noqa: E402
from sf_glass import GlassMixin  # noqa: E402
from tests._base import SFCase  # noqa: E402


# 参照时刻：**必须**贴着真实时钟取，不能写死成某个整点。
# 为什么（v1.4.51 实测的教训，台账 G14）：这里原来写的是 `datetime(2026, 10, 9, 12, 0, 0)`，
# 而 `GlassDiagnosticsCase` 走的是产品默认路径（`recent_log_anchors()` 不传 now，
# 由 `sf_logging` 自己取墙钟）。样本行是相对 now 生成的，于是"锚点扫到 3 类"这句话
# 实际在验"跑测试的真实时刻是否落在 12:00~12:30 这个巧合窗口里"——
# 12:12 那轮全量它是绿的（什么都没验到），12:39 之后开始红，明天起永远红。
# 改成 `datetime.now()` 之后两类断言都确定性成立：显式传 now 的靠相对偏移，
# 不传 now 的靠"样本就是刚刚生成的"。
NOW = datetime.datetime.now().replace(microsecond=0)


def _stamp(mom):
    return mom.strftime("%Y-%m-%d %H:%M:%S.") + "%03d" % (mom.microsecond // 1000)


def _line(mom, run, level, msg):
    return "[%s %s] %s: %s" % (_stamp(mom), run, level, msg)


def _sample_lines(now=NOW):
    """一份形状已知的日志行：三类锚点、两次运行、一行旧格式、一行很久以前。

    时刻全部**相对 now 生成**（AGENTS §6 第 4 条：不依赖"现在几点"）——
    窗口过滤的断言因此与跑测试的日期无关。
    """
    now = now or NOW
    old = now - datetime.timedelta(days=3)
    return [
        _line(now - datetime.timedelta(minutes=5), "a1b2c3", "INFO", "STARTUP v1.4.46 run=a1b2c3"),
        _line(now - datetime.timedelta(seconds=30), "a1b2c3", "INFO", "re-top: glass above main"),
        _line(now - datetime.timedelta(seconds=20), "a1b2c3", "INFO", "re-top: glass above main ×3"),
        _line(now - datetime.timedelta(seconds=10), "a1b2c3", "WARNING",
              "re-top: order probe blind: pair not both on-screen (main=41 glass=42)"),
        _line(now - datetime.timedelta(seconds=9), "d4e5f6", "ERROR",
              "SWALLOWED sf_glass._force_top: AttributeError('x')"),
        _line(old, "a1b2c3", "INFO", "re-top: glass above main"),
        "[15:29:36] INFO: bundled fonts loaded（旧格式：没有日期也没有 run id）",
    ]


def _sample_text(now=NOW):
    return "\n".join(_sample_lines(now)) + "\n"


class AnchorScanCase(SFCase):
    """防线三新添的这一环：锚点扫描本身不能成为又一枚会说假话的检查。"""

    def setUp(self):
        super().setUp()
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.log = self.tmp / "debug.log"
        self.log.write_text(_sample_text(), encoding="utf-8")
        self._old_log = sf_logging.LOG_PATH
        sf_logging.LOG_PATH = str(self.log)
        self.addCleanup(setattr, sf_logging, "LOG_PATH", self._old_log)

    def test_shapes_and_counters_are_facts_not_guesses(self):
        out = sf_logging.recent_log_anchors(minutes=30, path=str(self.log), now=NOW)
        self.assertFalse(out.get("error"), "读到了文件却报 error：%s" % out)
        got = {a["key"]: a for a in out["anchors"]}
        self.assertEqual(got["glass_above_main"]["hits"], 2)
        self.assertEqual(got["order_probe_blind"]["hits"], 1)
        self.assertEqual(got["swallowed"]["runs"], ["d4e5f6"])
        # 起点必须是该类**第一**行的时刻，不是最后一行
        self.assertEqual(got["glass_above_main"]["first_run"], "a1b2c3")
        first = datetime.datetime.fromisoformat(got["glass_above_main"]["first_seen"])
        last = datetime.datetime.fromisoformat(got["glass_above_main"]["last_seen"])
        self.assertLess(first, last)
        self.assertAlmostEqual((last - first).total_seconds(), 10.0, delta=1.0)
        # 旧格式行无法定位到绝对时刻，只能整段跳过 —— 但必须如实报数
        self.assertEqual(out["unparsed_lines"], 1)
        self.assertIn("a1b2c3", out["runs_seen"])
        self.assertIn("d4e5f6", out["runs_seen"])

    def test_time_window_excludes_old_episodes(self):
        """三天前那一段不能算进"最近 30 分钟"，但也不能被当成从没发生。"""
        near = sf_logging.recent_log_anchors(minutes=30, path=str(self.log), now=NOW)
        far = sf_logging.recent_log_anchors(minutes=99999, path=str(self.log), now=NOW)
        hits_near = {a["key"]: a["hits"] for a in near["anchors"]}["glass_above_main"]
        hits_far = {a["key"]: a["hits"] for a in far["anchors"]}["glass_above_main"]
        self.assertEqual(hits_near, 2)
        self.assertEqual(hits_far, 3)

    def test_missing_log_reports_error_not_emptiness(self):
        """扫不到东西和"根本没有日志"是两件事：后者必须写清楚。"""
        out = sf_logging.recent_log_anchors(path=str(self.tmp / "没有这个文件.log"), now=NOW)
        self.assertTrue(out.get("error"), "日志不存在却只返回空表：%s" % out)

    def test_future_dated_lines_are_not_counted_as_recent(self):
        """窗口是双向的：比参照时刻还晚的行不算"最近 30 分钟"（v1.4.51，G14 根因之一）。

        只写下界时，一条时钟超前的行（用户改过系统时间、日志来自另一台机器）会**永远**
        落在窗口内，把"三天前发生过"说成"刚刚还在发生" —— 而这句话是定位时长的起点。
        顺带验 5 分钟内的偏差仍然放行：写日志与读日志之间本就有秒级偏差。
        """
        far = _line(NOW + datetime.timedelta(hours=2), "a1b2c3", "INFO",
                    "re-top: glass above main")
        near = _line(NOW + datetime.timedelta(seconds=60), "d4e5f6", "WARNING",
                     "re-top: order probe blind: pair not both on-screen (main=1 glass=2)")
        path = self.tmp / "skew.log"
        path.write_text(far + "\n" + near + "\n", encoding="utf-8")
        out = sf_logging.recent_log_anchors(minutes=30, path=str(path), now=NOW)
        self.assertEqual(out["future_lines"], 1, "超前一小时的行必须被单独记账")
        keys = {a["key"] for a in out["anchors"]}
        self.assertNotIn("glass_above_main", keys,
                         "时钟超前的行被当成最近发生：%r" % keys)
        self.assertIn("order_probe_blind", keys,
                      "5 分钟内的偏差应当仍然算数（写读之间的正常抖动）")

    def test_tail_only_is_disclosed_as_truncated(self):
        big = (_sample_text() + "\n") * 200
        path = self.tmp / "big.log"
        path.write_text(big, encoding="utf-8")
        out = sf_logging.recent_log_anchors(minutes=99999, path=str(path), tail_bytes=600,
                                           now=NOW)
        self.assertTrue(out["truncated"], "只读了尾部却不报 truncated")
        self.assertLess(out["read_bytes"], len(big.encode("utf-8")))


class GlassDiagnosticsCase(SFCase):
    """诊断包里的 glass.json：离屏必须老实说"这一项没有值"，且不许段错误。"""

    class Host(GlassMixin):
        """只装配 glass_diagnostics 会读到的成员，不建窗口。

        为什么够用：这条方法只读属性，且在 native_cocoa 为假时**完全不碰 AppKit**；
        真值那一路由装机版与 tools/ 的真链路探针负责，离屏硬造只会造出一枚假门。
        """

    def setUp(self):
        super().setUp()
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.log = self.tmp / "debug.log"
        self.log.write_text(_sample_text(), encoding="utf-8")
        self._old_log = sf_logging.LOG_PATH
        sf_logging.LOG_PATH = str(self.log)
        self.addCleanup(setattr, sf_logging, "LOG_PATH", self._old_log)

    def _host(self):
        host = self.Host()
        host._glass_window = None      # discipline-ok(R5): 被测方法只读这几个自有成员
        host._retop_slots = {"glass above main": {"at": 0.0, "suppressed": 7}}  # discipline-ok(R5): 同上
        return host

    def test_offscreen_says_why_real_state_is_none(self):
        out = self._host().glass_diagnostics()
        # 断言的是"配对关系"而不是当前跑在哪个平台：真值那一路在装机版里才有意义，
        # 直接跑本模块（cocoa）时也不该因为平台不同而误红。
        if not out["native_cocoa"]:
            self.assertIsNone(out["real_state"], "离屏却给出了合成顺序结果 —— 那就是假数据")
        self.assertTrue(out["real_state_note"], "real_state 为空必须带上为什么")
        self.assertEqual(len(out["anchors"]["anchors"]), 3,
                         "锚点这一半在任何平台都能算出来，少一类就是漏")

    def test_retop_last_snapshot_ages_and_suppressed(self):
        """每种失效最后一次距今多久 + 压掉多少条：与日志锚点互为对照，两边不一致说明有一侧说谎。"""
        out = self._host().glass_diagnostics()
        rec = out["retop_last"]["glass above main"]
        self.assertEqual(rec["suppressed"], 7)
        self.assertGreater(rec["age_sec"], 0.0)

    def test_needles_stay_in_sync_with_product(self):
        """产品侧新增/改写锚点而工具没跟上时，这条先红，而不是让扫描悄悄漏掉。

        为什么用子进程取工具侧的表：这个工具在真实用法里只被命令行调用（--from-log /
        --from-diag 都由别的用例走 subprocess），把它 import 进测试主进程会让 665 条
        语句进覆盖率分母、而里面的 CLI 分支一行都不会在进程内跑 —— 实测把全仓覆盖率
        从 87% 拖到 83%，击穿 84% 下限。那是记账口径被测试自己污染，不是代码变坏了。
        """
        cp = subprocess.run([sys.executable, "-c",
                             "import json,sys;sys.path.insert(0,'tools');"
                             "import quality_trend as q;"
                             "print(json.dumps(q.LOG_ANCHORS, ensure_ascii=False))"],
                            capture_output=True, text=True, cwd=str(ROOT), timeout=120)
        self.assertEqual(cp.returncode, 0, cp.stderr[-400:])
        tool = json.loads(cp.stdout)
        self.assertEqual([k for k, _n, _d in sf_logging.GLASS_ANCHORS],
                         [row[0] for row in tool], "锚点类别表漂移")
        self.assertEqual([n for _k, n, _d in sf_logging.GLASS_ANCHORS],
                         [row[1] for row in tool],
                         "needle 漂移：会扫不到却报「没有锚点」")


class DiagRoundtripCase(SFCase):
    """`--from-diag` 必须能从回传的 zip 里读出现场，并给出能直接抄的开条命令。"""

    def _zip(self, with_glass=True, with_log=True):
        td = pathlib.Path(tempfile.mkdtemp())
        zp = td / "诊断.zip"
        anchors = {"window_minutes": 30, "read_bytes": 1234, "truncated": False,
                   "unparsed_lines": 0, "note": "锚点是程序检出的时刻",
                   "anchors": [{"key": "glass_above_main", "hits": 3,
                                "first_seen": "2026-10-09T09:12:03.400",
                                "last_seen": "2026-10-09T09:22:03.400",
                                "runs": ["beef01"], "first_run": "beef01",
                                "desc": "背景层压在内容之上（用户视角的「只剩背景」）",
                                "sample": "re-top: glass above main"}]}
        glass = {"version": "1.4.46", "run": "beef01", "native_cocoa": True,
                 "glass_present": True, "follow_timer_active": False,
                 "numbers": {"main": 41, "glass": 42},
                 "real_state": {"glass_on_top": True, "desync_px": 11.5},
                 "retop_last": {"glass above main": {"age_sec": 3.2, "suppressed": 2}},
                 "anchors": anchors}
        with zipfile.ZipFile(zp, "w") as z:
            if with_glass:
                z.writestr("glass.json", json.dumps(glass, ensure_ascii=False))
            if with_log:
                z.writestr("debug.log", _sample_text())
        return zp

    def _run(self, zp):
        cp = subprocess.run([sys.executable, str(ROOT / "tools" / "quality_trend.py"),
                             "--from-diag", str(zp)],
                            capture_output=True, text=True, cwd=str(ROOT), timeout=120)
        return cp.returncode, cp.stdout + cp.stderr

    def test_zip_with_field_evidence_yields_a_start_stamp(self):
        rc, out = self._run(self._zip())
        self.assertEqual(rc, 0, out)
        self.assertIn("玻璃在上=True", out)
        self.assertIn("2026-10-09T09:12:03", out)
        self.assertIn("--observed-at", out)
        self.assertIn("距今 3.2 秒", out)
        # 包里的 debug.log 也要复核一遍（30 天窗口），不然只看见 30 分钟
        self.assertIn("包里的 debug.log 复核", out)

    def test_start_stamp_is_directly_parseable_as_iso(self):
        """开条命令里的时刻必须能被 --open 直接吃下，否则定位时长起点会退回"敲命令那一刻"。"""
        rc, out = self._run(self._zip())
        self.assertEqual(rc, 0, out)
        token = out.split('--observed-at "')[1].split('"')[0]
        # 只验"能原样被 --open 吃下"，不写年份这类裸数字（AGENTS §6 第 6 条）
        self.assertEqual(datetime.datetime.fromisoformat(token).isoformat(), token)

    def test_old_package_says_loudly_it_has_no_field(self):
        """≤v1.4.45 的包没有 glass.json：必须说明白，不能扫出空表就当没发生。"""
        rc, out = self._run(self._zip(with_glass=False))
        self.assertEqual(rc, 0, out)
        self.assertIn("没有 glass.json", out)
        self.assertIn("不等于", out)

    def test_missing_zip_fails_instead_of_passing_empty(self):
        cp = subprocess.run([sys.executable, str(ROOT / "tools" / "quality_trend.py"),
                             "--from-diag", str(pathlib.Path(tempfile.mkdtemp()) / "无.zip")],
                            capture_output=True, text=True, cwd=str(ROOT), timeout=120)
        self.assertNotEqual(cp.returncode, 0, "包不存在却返回 0 —— 又是一枚永不失败的检查")


if __name__ == "__main__":
    unittest.main(verbosity=2)
