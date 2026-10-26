# -*- coding: utf-8 -*-
"""链上核验工具的 **CLI 表面**在测试主进程里跑（覆盖率轮 B 档，2026-10-12）。

为什么专门补这一层（实测）：`tools/` 那 1403 条未覆盖语句里最大一块是各工具的 `main()`
—— 合计 444 条。它们此前只在两种地方跑过：人敲一次、或发版链里起**子进程**，
而子进程不计入 `.coverage`（AGENTS §6 第 10 条）。`tests/test_gate_selftests_in_suite.py`
把 `self_test()` 搬进了本进程，但**没有搬 `main()`**，于是这一族缺陷一条都测不到：

  · 参数拼错时不退 2，而是滑进默认动作（`coverage_gate.py report` 漏两横杠 = 安静跑一次判定
    并把"通过"打在终端上，2026-10-12 全方位复核 F5 就是这条）；
  · `--update` 这类**会写基线**的分支，转发参数写错（`--allow-drop` 没接到）没人发现；
  · 分发写漏一条：`--report` 变成"什么都没做但退 0"。

这一档的取舍（写死成规矩，别下次再犹豫）：
  ✅ 跑：只读分支、以及"写"分支但把落点改到临时文件（`cmd_update(..., baseline_path=临时)`）；
  ❌ 不跑：任何会真挂 DMG 卷、真装 `/Applications`、真推 OTA、真落 `build/.gate_stamp`
     的分支 —— 那些是发版链 3.8/6 步的专属动作，进全量等于每次跑测试都碰用户机器。
     具体不跑的清单与理由写在下面 `NOT_HERE`。
  ⚠ 反向红线：每条"写"用例都必须断言**真基线文件的字节没变**，
     这条一旦哪天不成立，本文件本身就是污染源。

`main()` 全部接受 argv（除 `gate_stamp`/`dist_version_check` 收的是 `sys.argv[1:]` 形状），
所以这里不需要碰 `sys.argv`，也不会影响别的用例。

按 AGENTS §6：断言里没有版本号字面量（R1）、不读源码做字符串断言（R2）、不 sleep（R3）、
不依赖当下时钟（R4）、不访问私有成员（R5，只调公开函数与 `main`）、继承 SFCase（R7）。
"""
from __future__ import annotations

import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

try:
    from _base import SFCase                          # noqa: E402
except ImportError:                                    # pragma: no cover
    from tests._base import SFCase                    # noqa: E402

import coverage_gate as cg                              # noqa: E402
import test_discipline_scan as tds                     # noqa: E402
import release_feature_check as rfc                     # noqa: E402
import gate_stamp as gs                                 # noqa: E402
import bundle_closure_check as bcc                      # noqa: E402
import dist_version_check as dvc                        # noqa: E402
import verify_dmg_pyz as vdp                            # noqa: E402

REAL_COVERAGE_BASELINE = ROOT / "tools" / "coverage_gate_baseline.json"
REAL_DISCIPLINE_BASELINE = ROOT / "tools" / "test_discipline_baseline.json"

# 有意不在这里跑的分支（写清楚，别当成漏了）
NOT_HERE = {
    "gate_stamp write": "落的是 build/.gate_stamp —— 全量里落一枚"
                        "「跑过回归」的戳等于自己给自己发通行证，只能由 run_tests.sh 全绿后写",
    "coverage_gate --update（真基线）": "写 tools/coverage_gate_baseline.json，属发版侧动作；"
                                       "本文件只跑 baseline_path 指向临时副本的那条路径",
    "verify_install_paths": "会真解包安装到临时目录并挂 DMG 卷（发版链 3.8 单独跑）",
    "smoke_frozen_app": "要真起打包产物进程",
    "publish_update_release": "端到端发布另有 tests/test_publish_end_to_end.py 用本地假服务跑",
    "make_update_assets main": "要真 .app 目录 + ditto 出 zip，产物层已由发版链与 "
                              "tests/test_release_gates.py 的子进程档覆盖",
    "sweep_temp_apps": "默认动作会扫真实 /tmp 与 $TMPDIR 并改名，全量里不该碰系统临时目录",
    "static_gate 默认档": "要跑完整 ruff+mypy（≈20 秒），已由 run_tests.sh 第 0 关每轮真跑",
}

# ---- 合成覆盖率数据（cmd_report / cmd_update 的确定性输入）----
# 产品档：1000 条语句缺 100 → 90.00%；链上档：100 条缺 20 → 80.00%
PRODUCT_STMTS, PRODUCT_MISS = 1000, 100
CHAIN_STMTS, CHAIN_MISS = 100, 20
PRODUCT_OK_EXPECT = "产品代码 90.00%"
CHAIN_OK_EXPECT = "发版链 tools/ 80.00%"


def synthetic_data(product=None, chain=None, files=None, total="85"):
    return {"tiers": {"product": product if product is not None
                      else [PRODUCT_STMTS, PRODUCT_MISS, 10],
                      "chain": chain if chain is not None
                      else [CHAIN_STMTS, CHAIN_MISS, 3],
                      },
            "files": files if files is not None else [
                ("tools/a_bad.py", 50, 40),
                ("tools/b_ok.py", 50, 10),
                ("sf_x.py", 1000, 90),
                *[(name, 100, 0) for name in cg.REQUIRED_MODULES],
            ],
            "total_display": total}


def run(main, argv):
    """跑一次 CLI 表面，返回 (退出码, 合并后的输出)。SystemExit 归一成退出码。

    `raise SystemExit("一句话")` 的字符串**不会被打印**（只有在进程真退出时 Python 才打它），
    在本进程里跑 CLI 就必须自己把它收进输出 —— 否则"失败了但没话说"看起来像测试没测到。
    """
    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            rc = main(argv)
    except SystemExit as exc:               # argparse 的 error() 走 sys.exit(2)
        code = exc.code
        rc = code if isinstance(code, int) else 1
        if not isinstance(code, int):
            out.write("%s\n" % (code if code is not None else ""))
    return rc, out.getvalue() + err.getvalue()


def call(fn):
    """跑一个不带参数的可调用（直接调 cmd_* / read_totals 这类公开函数）并收输出。"""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = fn()
    return rc, out.getvalue() + err.getvalue()


PROG = "coverage_gate.py"          # coverage_gate.main 吃的是 sys.argv 形状：argv[0] 是程序名


def _pct(stmts, miss):
    return 100.0 * (stmts - miss) / stmts if stmts else -1.0


def make_coverage_data():
    """在仓库外跑一枚真 coverage，数据文件里只留"一枚产品文件 + 一枚链上文件"。

    为什么要真跑而不是手写 JSON：`read_totals` 吃的是 coverage 自己的数据格式，
    手工拼出来的文件测不到它真实的读法（含 `.coveragerc` 的 omit 与分档前缀）。
    """
    import subprocess
    td = pathlib.Path(tempfile.mkdtemp())
    runner = td / "runner.py"
    runner.write_text(
        "import sys\nsys.path.insert(0, %r); sys.path.insert(0, %r)\n"
        "import sf_constants, coverage_gate\n"
        "print(sf_constants.APP_VERSION is not None, hasattr(coverage_gate, 'judge'))\n"
        % (str(ROOT), str(ROOT / "tools")), encoding="utf-8")
    dat = td / "data.dat"
    proc = subprocess.run([sys.executable, "-m", "coverage", "run",
                           "--data-file", str(dat), str(runner)],
                          cwd=str(ROOT), capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-400:]
    return dat


class CoverageGateCli(SFCase):
    """`coverage_gate` 的参数层：未知参数必须退 2，`--update` 的开关必须转发到位。"""

    def setUp(self):
        super().setUp()
        self.real_bytes = REAL_COVERAGE_BASELINE.read_bytes()

    def tearDown(self):
        self.assertEqual(REAL_COVERAGE_BASELINE.read_bytes(), self.real_bytes,
                         "本文件把真基线写了 —— 棘轮的账本只能由人显式 --update 改")
        super().tearDown()

    def test_未知参数退2且不落默认动作(self):
        rc, out = run(cg.main, [PROG, "report"])          # 漏两横杠：曾经会安静滑进判定
        self.assertEqual(rc, 2, "无横杠的 report 滑进了默认动作 = F5 那枚缺陷回来了")
        self.assertIn("不认识参数", out)
        rc2, out2 = run(cg.main, [PROG, "--no-such"])
        self.assertEqual(rc2, 2)
        self.assertIn("--min=", out2)               # 用法行必须告诉人能用哪些参数

    def test_min_必须是整数(self):
        rc, out = run(cg.main, [PROG, "--min=abc"])
        self.assertEqual(rc, 2)
        self.assertIn("--min 需要整数", out)

    def test_基线不可用时退2(self):
        self.track_patch(mock.patch.object(cg, "load_baseline",
                                           side_effect=RuntimeError("基线不存在")))
        rc, out = run(cg.main, [PROG, "--report"])
        self.assertEqual(rc, 2, "基线读不到却继续判定 = 拿默认值发免罪券")
        self.assertIn("基线不可用", out)

    def test_self_test_参数确实分发到自检(self):
        self.track_patch(mock.patch.object(cg, "self_test", return_value=7))
        rc, _ = run(cg.main, [PROG, "--self-test"])
        self.assertEqual(rc, 7, "分发漏了 --self-test 就会滑进判定分支")

    def test_update_把allow_drop原样转发(self):
        seen = {}

        def fake_cmd_update(baseline, allow_drop=False, baseline_path=None):
            seen["allow_drop"] = allow_drop
            seen["floors"] = (baseline["product_floor"], baseline["chain_floor"])
            return 0

        self.track_patch(mock.patch.object(cg, "cmd_update", side_effect=fake_cmd_update))
        rc, _ = run(cg.main, [PROG, "--update"])
        self.assertEqual(rc, 0)
        self.assertFalse(seen["allow_drop"], "不带 --allow-drop 时绝不能把放宽开关传下去")
        run(cg.main, [PROG, "--update", "--allow-drop"])
        self.assertTrue(seen["allow_drop"], "--allow-drop 没转发 = 下调下限永远判红")

    def test_min_改写的是本次判定不是基线(self):
        baseline = {"product_floor": 94, "chain_floor": 87,
                    "module_floors": dict.fromkeys(cg.REQUIRED_MODULES, 90)}
        captured = {}

        self.track_patch(mock.patch.object(cg, "load_baseline", return_value=baseline))
        self.track_patch(mock.patch.object(
            cg, "load_data", return_value=(synthetic_data(product=[1000, 40, 10], chain=[100, 10, 10]), "")))
        rc, _ = run(cg.main, [PROG, "--min=95"])
        self.assertEqual(rc, 0)
        self.assertEqual(baseline["product_floor"], 95)
        self.assertEqual(REAL_COVERAGE_BASELINE.read_bytes(), self.real_bytes,
                         "--min 只该改本次门槛；写了基线就等于偷偷放宽棘轮")

    def test_report_分支用合成数据给确定结论(self):
        self.track_patch(mock.patch.object(cg, "load_data",
                                           return_value=(synthetic_data(), "")))
        baseline = {"product_floor": 84, "chain_floor": 63,
                    "module_floors": dict.fromkeys(cg.REQUIRED_MODULES, 90)}
        rc, out = call(lambda: cg.cmd_report(baseline))
        self.assertEqual(rc, 0, out)
        self.assertIn(PRODUCT_OK_EXPECT, out)
        self.assertIn(CHAIN_OK_EXPECT, out)
        self.assertIn("全仓合并值（仅作参照，不再当闸门）：85%", out)
        # 链上榜只许列 tools/，且按 miss 降序 —— 否则"抬链上档从这里挑"会指错地方
        listing = [l.strip() for l in out.splitlines() if l.strip().startswith("tools/")]
        self.assertEqual([l.split()[0] for l in listing],
                         ["tools/a_bad.py", "tools/b_ok.py"], out)
        self.assertIn("链上档低不等于没测", out)

    def test_report_拿不到数据时退2(self):
        self.track_patch(mock.patch.object(cg, "load_data", return_value=(None, "没有 .coverage")))
        rc, out = call(lambda: cg.cmd_report({"product_floor": 84, "chain_floor": 63}))
        self.assertEqual(rc, 2)
        self.assertIn("覆盖率数据不可用", out)

    def test_默认档通过时要带上记账口径提醒(self):
        self.track_patch(mock.patch.object(cg, "load_data",
                                           return_value=(synthetic_data(product=[1000, 40, 10],
                                                                        chain=[100, 10, 10]), "")))
        rc, out = run(cg.main, [PROG])
        self.assertEqual(rc, 0, out)
        self.assertIn("这是记账口径不是没测", out)

    def test_默认档判红时给出的是可操作的话(self):
        data = synthetic_data(product=[1000, 400, 10])      # 产品档实测 60%
        self.track_patch(mock.patch.object(cg, "load_data", return_value=(data, "")))
        rc, out = run(cg.main, [PROG])
        self.assertEqual(rc, 1)
        self.assertIn("红的时候先分清是哪一档", out)
        # 差值从**基线**取，不写死 24.00：那是"下限 84"时代的数字，2026-10-13 下限抬到 89
        # 之后它当场过期（本轮就是这么被自己的关卡抓到的）。要断言的是这句话的形状，
        # 不是某一天的下限值。
        floor = cg.load_baseline()["product_floor"]
        self.assertIn("差 %.2f 个百分点" % (floor - 60.0), out)
        self.assertIn("别把 %d 改小" % floor, out, "红字里劝人别改小的那个数，必须就是当前下限")

    def test_某档一个文件都没配上时不许算通过(self):
        data = synthetic_data(chain=[0, 0, 0])
        self.track_patch(mock.patch.object(cg, "load_data", return_value=(data, "")))
        rc, out = run(cg.main, [PROG])
        self.assertEqual(rc, 1, "空输入 = 整关通过，是 min_files 那一族的同类")
        self.assertIn("一个文件都没配上", out)

    def test_read_totals_按目录前缀分两档(self):
        """自己造一份 coverage 数据再读，绝不依赖仓库里现成的 .coverage ——
        跑测试期间它可能正被 `coverage erase` 清掉（工具自己的 docstring 就写了这一态）。"""
        dat = make_coverage_data()
        data = cg.read_totals(str(dat))
        tiers = data["tiers"]
        self.assertEqual(sorted(tiers), ["chain", "product"])
        for key in ("product", "chain"):
            stmts, miss, nfiles = tiers[key]
            self.assertGreater(nfiles, 0, "%s 一档配上 0 个文件 = 分档口径失效" % key)
            self.assertGreaterEqual(stmts, miss)
            if miss:
                self.assertLess(_pct(stmts, miss), 100.0)
        names = [f[0] for f in data["files"]]
        self.assertIn("sf_constants.py", names, "产品文件没进产品档 = 分档前缀写错")
        self.assertTrue(any(n.startswith("tools/") for n in names), "链上一档整条消失")
        self.assertFalse(any(n.startswith("tests/") for n in names), "tests/ 该被排除")

    def test_read_totals_没有数据文件时抛错而不是返回空(self):
        ghost = pathlib.Path(tempfile.mkdtemp()) / "no-such-data.dat"
        with self.assertRaises(RuntimeError):
            cg.read_totals(str(ghost))

    def test_cmd_update_只写临时副本并且升得上限(self):
        tmp = pathlib.Path(tempfile.mkdtemp()) / "baseline.json"
        baseline = {"product_floor": 84, "chain_floor": 63}
        self.track_patch(mock.patch.object(cg, "load_data",
                                           return_value=(synthetic_data(), "")))
        rc, out = call(lambda: cg.cmd_update(baseline, False, baseline_path=tmp))
        self.assertEqual(rc, 0, out)
        written = json.loads(tmp.read_text(encoding="utf-8"))
        self.assertEqual(written["chain_floor"], 80, "链上实测 80% 应把下限抬到 80（向下取整）")
        self.assertEqual(written["measured_product"], 90.0)
        self.assertEqual(REAL_COVERAGE_BASELINE.read_bytes(), self.real_bytes)

    def test_cmd_update_下调必须显式放行(self):
        tmp = pathlib.Path(tempfile.mkdtemp()) / "baseline.json"
        baseline = {"product_floor": 84, "chain_floor": 95}
        self.track_patch(mock.patch.object(cg, "load_data",
                                           return_value=(synthetic_data(), "")))
        rc, out = call(lambda: cg.cmd_update(baseline, False, baseline_path=tmp))
        self.assertEqual(rc, 1, "下调链上下限没被拦 = 一条环境变量就能放宽棘轮")
        self.assertIn("--allow-drop", out)
        self.assertFalse(tmp.exists(), "拒绝写入时不该留下半个基线")
        rc2, out2 = call(lambda: cg.cmd_update(baseline, True, baseline_path=tmp))
        self.assertEqual(rc2, 0, out2)
        self.assertIn("放宽闸门", out2, "显式放行也必须大声留痕")
        self.assertEqual(json.loads(tmp.read_text(encoding="utf-8"))["chain_floor"], 80)

    def test_cmd_update_没有数据时拒绝写(self):
        tmp = pathlib.Path(tempfile.mkdtemp()) / "baseline.json"
        self.track_patch(mock.patch.object(cg, "load_data", return_value=(None, "没有 .coverage")))
        rc, out = call(lambda: cg.cmd_update({"product_floor": 84, "chain_floor": 63}, False,
                                             baseline_path=tmp))
        self.assertEqual(rc, 2)
        self.assertIn("拒绝写基线", out)
        self.assertFalse(tmp.exists())

    def test_cmd_update_某档没有文件时拒绝写(self):
        tmp = pathlib.Path(tempfile.mkdtemp()) / "baseline.json"
        self.track_patch(mock.patch.object(cg, "load_data",
                                           return_value=(synthetic_data(chain=[0, 0, 0]), "")))
        rc, out = call(lambda: cg.cmd_update({"product_floor": 84, "chain_floor": 63}, False,
                                             baseline_path=tmp))
        self.assertEqual(rc, 1, "链上一档空着还写基线 = 把闸门清空")
        self.assertIn("没配上文件", out)
        self.assertFalse(tmp.exists())


class DisciplineScanCli(SFCase):
    """`test_discipline_scan` 的参数层：第 0 关每轮真跑默认档，这里补 report / 范围红线 / 未知参数。"""

    def setUp(self):
        super().setUp()
        self.real_bytes = REAL_DISCIPLINE_BASELINE.read_bytes()

    def tearDown(self):
        self.assertEqual(REAL_DISCIPLINE_BASELINE.read_bytes(), self.real_bytes,
                         "书写纪律基线被本文件改写了")
        super().tearDown()

    def test_report_打满七条规则(self):
        rc, out = run(tds.main, ["--report"])
        self.assertEqual(rc, 0, out)
        for rule in tds.RULES:
            self.assertIn("%s 合计" % rule, out, "少了 %s 这条规则的分布 = 有一整条规则脱账" % rule)

    def test_扫描范围太小必须拒答(self):
        tiny = pathlib.Path(tempfile.mkdtemp())
        (tiny / "test_a.py").write_text("import unittest\n", encoding="utf-8")
        for extra in ([], ["--report"]):
            rc, out = run(tds.main, ["--dir", str(tiny)] + extra)
            self.assertEqual(rc, 2, "空/极小输入退 0 = 关卡范围失效却报通过")
            self.assertIn("拒绝给出", out)

    def test_默认档在当前仓库必须绿(self):
        # 这条同时是给本仓新写用例兜底的：违反了 §6 又没写 discipline-ok，这里先红
        rc, out = run(tds.main, [])
        self.assertEqual(rc, 0, out)

    def test_quiet_只是少打字不改结论(self):
        rc, out = run(tds.main, ["--quiet"])
        self.assertEqual(rc, 0, out)
        self.assertNotIn("测试书写纪律：R1~R7", out)

    def test_未知参数退2(self):
        rc, _ = run(tds.main, ["--nope"])
        self.assertEqual(rc, 2)


class FeatureCheckCli(SFCase):
    """`release_feature_check` 的清单解析与放行/拒绝话术（发版链 3.6 那道闸门的门面）。"""

    def _checks(self, text):
        p = pathlib.Path(tempfile.mkdtemp()) / "release_checks.txt"
        p.write_text(text, encoding="utf-8")
        self.addCleanup(p.unlink)
        return p

    def test_没有app路径时必须退2并给出办法(self):
        rc, out = run(rfc.main, [])
        self.assertEqual(rc, 2)
        self.assertIn("需要 .app 路径", out)

    def test_清单条数按四种指令分别计数(self):
        p = self._checks("version 9.9.9\n"
                         "check sf_quote::_poll\n"
                         "check sf_quote::_apply_quote\n"
                         "forbid sf_quote::_gone\n"
                         "checkfile updater/update_helper.sh::CFBundleExecutable\n"
                         "forbidfile updater/update_helper.sh::StockFloatTargetPrice\n")
        parsed = rfc.parse_checks(p)
        self.assertEqual(parsed["check"], ["sf_quote::_poll", "sf_quote::_apply_quote"])
        self.assertEqual(parsed["forbid"], ["sf_quote::_gone"])
        self.assertEqual(parsed["checkfile"], ["updater/update_helper.sh::CFBundleExecutable"])
        self.assertEqual(parsed["forbidfile"], ["updater/update_helper.sh::StockFloatTargetPrice"])
        self.assertEqual(parsed["errors"], [])
        rc, out = run(rfc.main, [str(ROOT / "并不存在.app"), "--checks", str(p),
                                 "--version", "9.9.9"])
        self.assertIn("check 2 条 / forbid 1 条 / checkfile 1 条 / forbidfile 1 条", out)

    def test_井号后面是注释不进气泡串(self):
        p = self._checks("version 9.9.9\ncheck sf_quote::_poll  # 本轮改的是分时线程\n")
        parsed = rfc.parse_checks(p)
        self.assertEqual(parsed["check"], ["sf_quote::_poll"],
                         "含 # 的判据串会被当注释截断 —— 填完必须核对读到的条数（AGENTS §3.1）")

    def test_少了双冒号形状必须当场报错而不是静默丢弃(self):
        p = self._checks("version 9.9.9\ncheck sf_quote._poll\n")
        parsed = rfc.parse_checks(p)
        self.assertEqual(parsed["check"], [])
        self.assertTrue(parsed["errors"], "形状错的清单行被静默吞掉 = 清单比看着短")

    def test_不认识指令必须报错(self):
        p = self._checks("version 9.9.9\ncheckd sf_quote::_poll\n")
        self.assertTrue(rfc.parse_checks(p)["errors"])

    def test_两个version行是错误(self):
        p = self._checks("version 9.9.9\nversion 9.9.10\n")
        self.assertTrue(any("第二个 version" in e for e in rfc.parse_checks(p)["errors"]))

    def test_清单里没有特征时拒绝出包(self):
        p = self._checks("version 9.9.9\n")
        rc, out = run(rfc.main, [str(ROOT / "build"), "--checks", str(p), "--version", "9.9.9"])
        self.assertEqual(rc, 1, "只有一行 version 的清单足以证明本轮改动 = 闸门是枚摆设")
        self.assertIn("不足以证明本轮改动", out)

    def test_版本行与本轮不同源时拒绝(self):
        p = self._checks("version 9.9.9\ncheck sf_quote::_poll\n")
        rc, out = run(rfc.main, [str(ROOT / "build"), "--checks", str(p), "--version", "9.9.10"])
        self.assertEqual(rc, 1)
        self.assertIn("要么清单还是上一轮的", out)

    def test_申报跳过时必须写版本与理由(self):
        # 三种不合格写法都必须被拒，且**不算生效**（否则等于给以后每一版发免检条，G26）
        for text in ("stale-feature 只写了理由没写版本\n", "stale-feature\n",
                     "stale-feature 9.9.9\n"):
            p = self._checks("version 9.9.9\n" + text)
            declared, problems, other = rfc.stale_declaration(rfc.parse_checks(p), "9.9.9")
            self.assertTrue(problems, "%s 应当被拒（申报可以绕，但绕了必须写明对哪一版+为什么）"
                            % text.strip())
            self.assertEqual(declared, [], "不合规的申报不该算生效")
            self.assertEqual(other, [])

    def test_写全了理由就生效并大声打warning(self):
        p = self._checks("version 9.9.9\nstale-feature 9.9.9 本轮只动发布侧脚本，包内无新名字\n")
        declared, problems, other = rfc.stale_declaration(rfc.parse_checks(p), "9.9.9")
        self.assertEqual(problems, [], "写全了理由反而被拒 = 没人会再写这一行")
        self.assertEqual(len(declared), 1)
        self.assertEqual(other, [])
        rc, out = run(rfc.main, [str(ROOT / "并不存在.app"), "--checks", str(p),
                                 "--version", "9.9.9"])
        self.assertIn("WARNING 本轮按清单申报跳过", out)
        self.assertIn("只剩", out)
        self.assertNotEqual(rc, 0, "清单里没有 check/forbid 却被放行")

    def test_属于别版的申报本轮不生效但仍要说出来(self):
        p = self._checks("version 9.9.9\nstale-feature 9.9.8 上一版的外包改动\n"
                         "check sf_quote::_poll\n")
        declared, problems, other = rfc.stale_declaration(rfc.parse_checks(p), "9.9.9")
        self.assertEqual(problems, [])
        self.assertEqual(declared, [], "别版的申报被本轮继承 = 堆叠历史里丢了作用域（G26）")
        self.assertEqual(len(other), 1)
        rc, out = run(rfc.main, [str(ROOT / "并不存在.app"), "--checks", str(p),
                                 "--version", "9.9.9"])
        self.assertIn("别的版本，本轮不生效", out)

    def test_显式开关只跳过新增特征这一条(self):
        """AGENTS §3.1 的口径：`--allow-stale-feature` 只跳过"新增特征"判据，其余一条都不动。
        2026-10-12 复核发现旧实现把 problems 整表清空 —— 清单只剩一行 version 时连
        .app 不存在都退 0。改坏方向：把"不清 problems"改回"整表清空"，这条立刻红。"""
        p = self._checks("version 9.9.9\n")
        rc, out = run(rfc.main, [str(ROOT / "并不存在.app"), "--checks", str(p),
                                 "--version", "9.9.9", "--allow-stale-feature"])
        self.assertIn("WARNING 已用 --allow-stale-feature", out)
        self.assertEqual(rc, 1, "清单里一条 check 都没有：开关只免检新增特征，免不了清单形状")
        self.assertIn("不足以证明本轮改动", out)

    def test_开关放行后其余判据照常走完(self):
        """清单本身合规时，开关放行后要继续做包内资源与字节码核验 —— 不是整个闸门放假。"""
        p = self._checks("version 9.9.9\ncheck sf_quote::_poll\n")
        rc, out = run(rfc.main, [str(ROOT / "并不存在.app"), "--checks", str(p),
                                 "--version", "9.9.9", "--allow-stale-feature"])
        self.assertIn("WARNING 已用 --allow-stale-feature", out)
        self.assertNotEqual(rc, 0, "app 不存在却退 0 = 连产物核验都被跳过了")


class GateStampCli(SFCase):
    """`gate_stamp` 的只读子命令；`write` 有意不跑（见文件头 NOT_HERE）。"""

    def test_path_子命令给出真实落点(self):
        rc, out = run(gs.main, ["path"])
        self.assertEqual(rc, 0)
        self.assertTrue(out.strip().endswith(os.path.join("build", ".gate_stamp")), out)

    def test_check_的措辞必须与退出码一致(self):
        """状态本身随工作区变，所以断言的不是"可用/不可用"，而是"说的话与退的码一致"。"""
        rc, out = run(gs.main, ["check"])
        self.assertIn(rc, (0, 1), out)
        if rc == 0:
            self.assertIn("关卡时间戳可用", out)
            self.assertNotIn("不可用", out)
        else:
            self.assertIn("不可用", out)

    def test_拼错的子命令退2而不是落戳(self):
        rc, out = run(gs.main, ["wrote"])
        self.assertEqual(rc, 2, "默认动作是落关卡时间戳：拼错当成授权等于自己给自己发通行证")
        self.assertIn("不认识参数", out)

    def test_空参数打用法并退2(self):
        rc, out = run(gs.main, [])
        self.assertEqual(rc, 2)
        self.assertIn("用法", out)


class BundleClosureCli(SFCase):
    def test_不给产物路径也不给self_test时退2(self):
        rc, out = run(bcc.main, [])
        self.assertEqual(rc, 2)
        self.assertIn("--self-test", out)

    def test_产物不存在时退2(self):
        rc, out = run(bcc.main, [str(ROOT / "并不存在.app")])
        self.assertEqual(rc, 2, out)
        self.assertIn("产物不存在", out)

    def test_假产物必须判红而不是判通过(self):
        fake = pathlib.Path(tempfile.mkdtemp()) / "Fake.app"
        (fake / "Contents" / "MacOS").mkdir(parents=True)
        rc, out = run(bcc.main, [str(fake)])
        self.assertNotEqual(rc, 0, "空壳产物被判成闭包完整 = v1.3.74 那一枚又回来了")
        self.assertIn("Info.plist", out)


class VersionAndPyzCli(SFCase):
    """`dist_version_check` / `verify_dmg_pyz` 的失败面：防拿旧产物打新 DMG 的那两道。"""

    def test_dist_version_缺参数时打usage(self):
        rc, out = run(dvc.main, ["only-one"])
        self.assertEqual(rc, 2)
        self.assertIn("usage", out)

    def test_dist_version_版本不符时判红(self):
        fake = pathlib.Path(tempfile.mkdtemp()) / "Fake.app"
        fake.mkdir(parents=True)
        rc, out = run(dvc.main, [str(fake), "9.9.9"])
        self.assertEqual(rc, 1, out)
        self.assertIn("MISMATCH", out)

    def test_verify_dmg_pyz_对不存在产物给结论不给栈(self):
        rc, out = run(vdp.main, [str(ROOT / "并不存在.app")])
        self.assertNotEqual(rc, 0, out)
        self.assertNotIn("Traceback", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
