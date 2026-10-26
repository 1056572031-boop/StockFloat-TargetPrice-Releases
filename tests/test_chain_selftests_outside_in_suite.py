# -*- coding: utf-8 -*-
"""链上工具的**第二层**：没进 IN_SUITE 的自检、静态闸门的分发、发包工具的早退闸门。

这一档挑的都是"有真判据、但在能记账的地方从来没跑过"的代码，逐块说清为什么：

  · `tools/test_runner.py::self_test`（59 条语句）—— 它验的是**退出码怎么算**：
    用户数据被改（2）＞ 失败（1）＞ 只有 flaky（0）。这套优先级一旦写错，
    红/绿本身就开始说谎。它此前只在 `SF_TEST_RUNNER_SELFTEST` 那种人工调用里跑过；
    `test_gate_selftests_in_suite.py` 把它列进 SKIPPED，理由是"它就是运行器，自跑无意义" ——
    那句理由针对的是"跑第二遍全量"，而 `self_test()` 刻意不跑用例，只喂形状。
  · `tools/publish_update_release.py::self_test`（41 条）—— 守的是台账里真实逃逸过的
    两件事：URL 必须是纯 ASCII（v1.3.72~1.3.79 外机点下载即失败），
    以及"没验证到资产就不许切清单指针"。同一份 SKIPPED 理由把它挡在账外。
  · `tools/static_gate.py::main`（63 条）—— 第一道防线的**分发层**：`--report`/`--update`/
    默认判定/未知参数。这里全部喂合成 findings 并 patch 掉真检器，
    所以既不会把 ruff+mypy 的 20 秒塞进全量，也不会写 `static_gate_baseline.json`。
  · `tools/make_update_assets.py::main`（58 条）—— 只跑到**早退闸门**为止：
    包名、bundle 标识、版本号、发布说明有没有本节标题。这四道之后就要读本机签名私钥
    并 ditto 出真 zip —— 那半段留在发版链里跑，本文件一行都不碰（见 UNDER_TEST）。

三条硬约束（写在用例里，不是写在文档里）：
  1. 任何"写基线"的分支必须 patch 掉真实写入，且用例外比对真基线文件的字节没变；
  2. 不许读 `.private_tools/update_signing_key.txt`（本机私钥）—— 所以 `make_update_assets`
     的用例全部止步于签名之前的那道闸门，并断言它确实是被那道闸门拦下来的；
  3. 不许联网：`publish_update_release` 只用它的纯 URL 拼装函数，不发请求。

AGENTS §6 对号：断言里没有版本号字面量（R1；夹具版本写成模块常量再引用）、
不读源码做字符串断言（R2）、不 sleep（R3）、不碰私有成员（R5）。
"""
from __future__ import annotations

import io
import os
import pathlib
import plistlib
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

import test_runner as tr                              # noqa: E402
import publish_update_release as pub                  # noqa: E402
import static_gate as sg                               # noqa: E402
import make_update_assets as mua                      # noqa: E402
import bundle_closure_check as bcc                    # noqa: E402
import sf_constants as sfc                            # noqa: E402
from tools import quality_evidence as quality         # noqa: E402
import diag_coverage_check as diag                    # noqa: E402

REAL_STATIC_BASELINE = ROOT / "tools" / "static_gate_baseline.json"

# 夹具版本号：写成常量，断言里不出现裸版本号字面量（AGENTS §6 第 1 条）
V_NO_SECTION = "9.9.90"
V_FAKE_APP = "9.9.91"

UNDER_TEST = {
    "make_update_assets 的后半段": "再往下要读本机更新签名私钥并 ditto 出真资产包，"
                                   "那是发版链第 6 步的动作；本文件只验到签名之前的四道早退闸门",
    "static_gate 的默认检器": "真跑 ruff+mypy ≈20 秒，已由 run_tests.sh 第 0 关每轮真跑；"
                             "这里只喂合成 findings 验分发与判定层",
}


def call(fn):
    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            rc = fn()
    except SystemExit as exc:
        code = exc.code
        rc = code if isinstance(code, int) else 1
        if not isinstance(code, int):
            out.write("%s\n" % (code or ""))
    return rc, out.getvalue() + err.getvalue()


class QualityAndDiagnosticToolSelfChecks(SFCase):
    def test_quality_evidence_self_test_runs_in_the_parent_process(self):
        rc, out = call(quality.self_test)
        self.assertEqual(rc, 0, out)
        self.assertIn("artifact tamper", out)

    def test_diag_coverage_self_test_runs_in_the_parent_process(self):
        self.assertEqual(diag.self_test(ROOT), 0)


class TestRunnerVerdictSelfCheck(SFCase):
    """运行器自己的判定层必须能在本进程里被跑红/跑绿。"""

    def test_退出码优先级自检通过(self):
        rc, out = call(tr.self_test)
        self.assertEqual(rc, 0, out)
        self.assertIn("self-test 通过", out)

    def test_用户数据红线压过失败(self):
        """优先级写反的话，"测试挂了 + 改了用户数据"会只报 1，而 2 才是那条不能吞的红线。"""
        self.assertEqual(tr.verdict([], [], [], []), 0)
        self.assertEqual(tr.verdict([("t", "boom")], [], [], []), 1)
        self.assertEqual(tr.verdict([], [("t", "flaky")], [], []), 0,
                         "重跑过了的抖动不该算失败")
        self.assertEqual(tr.verdict([("t", "boom")], [], [], ["config.json 变了"]), 2,
                         "用户数据被改必须优先于用例失败")

    def test_弹窗违规算进失败(self):
        """桌面凭空多出一个框，是用户能看见的缺陷 —— 它不能只打一行字就过去。"""
        self.assertEqual(tr.verdict([], [], [("t", "osascript -e display dialog")], []), 1)

    def test_seed_解析的四种输入(self):
        self.assertEqual(tr.parse_seed(None), 0)
        self.assertEqual(tr.parse_seed("20261009"), 20261009)
        with self.assertRaises(SystemExit) as cm:
            tr.parse_seed("20261008b")
        self.assertIn("SF_RANDOM_SEED", str(cm.exception),
                      "非法 seed 的报错得说清是哪个环境变量，否则又是一轮猜谜")


class PublishReleasePureLayer(SFCase):
    """发布侧的纯函数：URL 必须纯 ASCII，未验证到资产必须拒绝切清单。"""

    def test_发布器自检通过(self):
        rc, out = call(pub.self_test)
        self.assertEqual(rc, 0, out)

    def test_三条对外URL都是纯ASCII(self):
        """v1.3.72~1.3.79 的真实逃逸：固定标签是中文，Python 3.14 的 urllib
        在未编码时抛 UnicodeEncodeError —— 用户点"下载"就报失败，而清单能下能验签。"""
        self.assertTrue(pub.release_url().isascii(), repr(pub.release_url()))
        self.assertTrue(pub.contents_url().isascii(), repr(pub.contents_url()))
        encoded = pub.upload_url("123", "实时股价-1.4.46-macos.zip")
        self.assertTrue(encoded.isascii(), "中文资产名没编码就拼进 query")
        self.assertIn("/assets?", encoded)
        self.assertNotEqual(pub.upload_url("1", "a.zip"), pub.upload_url("2", "a.zip"),
                            "不同 release 得到同一个上传地址 = 会传到别版本上去")

    def test_没验证到资产时拒绝切换清单指针(self):
        tmp = pathlib.Path(tempfile.mkdtemp()) / "StockFloatTargetPrice-1.4.46-macos.zip"
        tmp.write_bytes(b"x" * 1234)
        ok = {"assets": [{"name": tmp.name, "size": 1234, "browser_download_url": "u"}]}
        self.assertIsNotNone(pub.verified_asset(ok, tmp),
                             "名字与大小都匹配的资产被判成不存在 = 正常发版被自己拦死")
        for broken in ({"assets": [{"name": tmp.name, "size": 1233}]},
                       {"assets": [{"name": "别版.zip", "size": 1234}]},
                       {"assets": []}, {}):
            with self.assertRaises(SystemExit):
                pub.verified_asset(broken, tmp)

    def test_读回结论区分CDN滞后与真没发上去(self):
        """把 CDN 滞后判成失败会中止一次正常发版；把上一版判成通过就是读回白做。"""
        ok, lines = pub.readback_verdict({"version": V_NO_SECTION}, V_NO_SECTION,
                                         {"version": "9.9.89"})
        self.assertTrue(ok, lines)
        self.assertTrue(any("CDN" in x for x in lines), "raw 滞后必须如实说出来")
        bad, _ = pub.readback_verdict({"version": "9.9.89"}, V_NO_SECTION)
        self.assertFalse(bad, "仓库里还是上一版却被判通过")


class StaticGateCli(SFCase):
    """静态闸门的分发与判定层：喂合成 findings，不跑真检器、不写真基线。"""

    def setUp(self):
        super().setUp()
        self.real_bytes = REAL_STATIC_BASELINE.read_bytes()

    def tearDown(self):
        self.assertEqual(REAL_STATIC_BASELINE.read_bytes(), self.real_bytes,
                         "B 级基线被本文件改写了 —— 棘轮只能由人显式 --update 动")
        super().tearDown()

    def _run(self, argv, hard=None, ratchet=None, hard_raw="", boom=None,
             baseline=None, write_raises=None):
        def fake_collect():
            if boom:
                raise boom
            return (hard or {}), (ratchet or {}), hard_raw

        writes = {}

        def fake_write(counts, allow_rise=False):
            if write_raises:
                raise write_raises
            writes["counts"] = counts
            writes["allow_rise"] = allow_rise
            return None

        self.track_patch(mock.patch.object(sg, "collect", fake_collect))
        self.track_patch(mock.patch.object(sg, "write_baseline", fake_write))
        self.track_patch(mock.patch.object(
            sg, "load_baseline",
            lambda: baseline or {"total": 0, "counts": {}}))
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = sg.main(argv)
        return rc, out.getvalue() + err.getvalue(), writes

    def test_检器跑不动时退2而不是判通过(self):
        """v1.4.51 那枚事故的原形：`python -m mypy` 在没装 mypy 的解释器上恰好也退 1，
        旧实现把它当"有 finding"，整条类型层静默归零却照样打印通过。"""
        rc, out, _ = self._run([], boom=RuntimeError("mypy 不可用"))
        self.assertEqual(rc, 2)
        self.assertIn("不算通过", out)

    def test_report_按条数降序打印两级(self):
        rc, out, _ = self._run(["--report"],
                               hard={"F821:sf_x.py": 2},
                               ratchet={"F401:sf_a.py": 5, "F401:sf_b.py": 1})
        self.assertEqual(rc, 0, out)
        self.assertIn("A 级 2 条；B 级合计 6 条 / 2 个组合", out)
        first = out.index("F401:sf_a.py")
        self.assertLess(first, out.index("F401:sf_b.py"), "分布必须按条数降序，否则挑不动")

    def test_update_默认拒绝上调并原样转发allow_rise(self):
        rc, _, writes = self._run(["--update"], ratchet={"F401:sf_a.py": 1})
        self.assertEqual(rc, 0)
        self.assertFalse(writes["allow_rise"], "不带 --allow-rise 时绝不能把放宽传下去")
        rc2, _, writes2 = self._run(["--update", "--allow-rise"], ratchet={"F401:sf_a.py": 1})
        self.assertEqual(rc2, 0)
        self.assertTrue(writes2["allow_rise"])

    def test_update_写入被拒时给出人话并退1(self):
        rc, out, _ = self._run(["--update"], ratchet={"F401:sf_a.py": 9},
                               write_raises=RuntimeError("基线某键被上调"))
        self.assertEqual(rc, 1)
        self.assertIn("基线某键被上调", out)

    def test_默认档A级命中必须判红并逐条列出(self):
        rc, out, _ = self._run([], hard={"F821:sf_x.py": 1},
                               hard_raw="sf_x.py:10:1: F821 undefined name 'foo'",
                               baseline={"total": 0, "counts": {}})
        self.assertEqual(rc, 1, out)
        self.assertIn("F821 undefined name", out)

    def test_默认档新增棘轮错误时附上改法(self):
        baseline = {"total": 1, "counts": {"F401:sf_a.py": 1}}
        rc, out, _ = self._run([], hard={}, ratchet={"F401:sf_a.py": 2},
                               baseline=baseline)
        self.assertEqual(rc, 1)
        self.assertIn("棘轮只降不升", out)
        self.assertIn("--update", out, "红的时候要告诉人下一步，而不是只说个数")

    def test_存量减少时只建议不下调(self):
        baseline = {"total": 5, "counts": {"F401:sf_a.py": 5}}
        rc, out, _ = self._run([], hard={}, ratchet={"F401:sf_a.py": 2},
                               baseline=baseline)
        self.assertEqual(rc, 0, out)
        self.assertIn("建议下调基线", out)
        self.assertNotIn("已写入", out)

    def test_基线里没有的mypy错误族要提示一次(self):
        """`NEW_MYPY_CODES` 是 `collect()` 跑的时候才填的集合，全新进程里它是空的 ——
        所以这里把"这次出现了一族基线里没有的错"这个状态直接喂进判据层。
        这条锁的是：新错误族不许被静默吸收（白名单那版就漏过这一族）。"""
        self.track_patch(mock.patch.object(sg, "NEW_MYPY_CODES", {"arg-type"}))
        baseline = {"total": 0, "counts": {"mypy:sf_a.py:attr-defined": 1}}
        rc, out, _ = self._run([], hard={}, ratchet={"mypy:sf_a.py:arg-type": 1},
                               baseline=baseline)
        self.assertIn("基线里没有的 mypy 错误族", out)
        self.assertIn("arg-type", out)
        self.assertEqual(rc, 1)

    def test_quiet_少打字但不改结论(self):
        rc, out, _ = self._run(["--quiet"], hard={}, ratchet={},
                               baseline={"total": 0, "counts": {}})
        self.assertEqual(rc, 0)
        self.assertNotIn("静态闸门：A 级", out)

    def test_未知参数退2(self):
        with self.assertRaises(SystemExit) as cm:
            sg.main(["--nope"])
        self.assertEqual(cm.exception.code, 2)


class MakeUpdateAssetsEarlyGates(SFCase):
    """发包工具的早退闸门：包名 / bundle 标识 / 版本号 / 发布说明有没有本节标题。"""

    def _fake_app(self, name=None, bundle_id=None, version=None):
        """plist 里的两个值按参数原样写：空版本号是**要验的输入**，不能被默认值吃掉。"""
        td = pathlib.Path(tempfile.mkdtemp())
        app = td / (name or mua.APP_NAME)
        (app / "Contents" / "MacOS").mkdir(parents=True)
        plist = {"CFBundleIdentifier": bundle_id if bundle_id is not None
                 else mua.BUNDLE_ID,
                 "CFBundleShortVersionString": version if version is not None
                 else V_NO_SECTION}
        (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps(plist))
        return app, plist

    def _run(self, app, output):
        argv = [str(ROOT / "tools" / "make_update_assets.py"), str(app),
                "--output", str(output)]
        self.track_patch(mock.patch.object(sys, "argv", argv))
        return call(mua.main)

    def test_不是目标应用包时拒绝(self):
        other, _ = self._fake_app(name="SomethingElse.app")
        rc, out = self._run(other, pathlib.Path(tempfile.mkdtemp()))
        self.assertNotEqual(rc, 0)
        self.assertIn("不是目标应用包", out)

    def test_bundle标识不符时拒绝(self):
        app, _ = self._fake_app(bundle_id="com.example.other")
        rc, out = self._run(app, pathlib.Path(tempfile.mkdtemp()))
        self.assertNotEqual(rc, 0)
        self.assertIn("应用标识或版本无效", out)

    def test_版本号缺失时同样拒绝(self):
        app, _ = self._fake_app(version="")
        rc, out = self._run(app, pathlib.Path(tempfile.mkdtemp()))
        self.assertNotEqual(rc, 0)
        self.assertIn("应用标识或版本无效", out)

    def test_发布说明里没有本节标题时在读私钥之前就停(self):
        """这条是本轮唯一"跑到签名前"的用例：夹具版本在 release_notes.txt 里没有
        自己的一节 → 必须被 `require_own_section` 拦下。拦不住就等于用例会去读
        本机更新签名私钥并 ditto 出真资产 —— 那是发版动作，不该发生在跑测试时。"""
        out_dir = pathlib.Path(tempfile.mkdtemp())
        app, _ = self._fake_app()
        rc, out = self._run(app, out_dir)
        self.assertNotEqual(rc, 0, "没被早退闸门拦下 —— 用例已经伸手碰签名私钥了")
        self.assertIn("发布说明", out)
        self.assertEqual(list(out_dir.glob("*")), [], "拒绝之前不该在输出目录里留下半个资产")


class BundleAvailableLayer(SFCase):
    """`bundle_available`：归档条目 ∪ Frameworks/Resources/MacOS 的顶层名。

    这一层只能拿**真产物**验：它读的是 PyInstaller 的归档 TOC，假目录会在
    `CArchiveReader` 那一步直接抛 ArchiveReadError（实测）—— 而那恰好说明
    "假产物骗不过去"。真产物不在位时如实 skip 并说理由，不假装通过（AGENTS §6 第 10 条）。
    """

    APP = pathlib.Path("/Applications") / (sfc.APP_DISPLAY_NAME + ".app")

    def test_真产物里磁盘扩展与归档条目都算可用(self):
        if not self.APP.is_dir():
            self.skipTest("这台机器上没有装机版（%s 不在）—— 归档 TOC 层无从核验" % self.APP)
        avail = bcc.bundle_available(self.APP)
        self.assertGreater(len(avail), 50, "读到的顶层名太少 = TOC 解析退化")
        self.assertIn("PyQt6", avail)
        self.assertIn("_cffi_backend", avail,
                      "v1.3.74 漏的正是这一枚：.so 这类扩展的名字必须进集合，"
                      "否则装机版点「检查更新」就中断")

    def test_check_把缺失模块列出来而不是静默(self):
        """闭包对拍：产品源码里 import 了、包内没有的名字必须一条条列出来。"""
        if not self.APP.is_dir():
            self.skipTest("这台机器上没有装机版（%s 不在）" % self.APP)
        src = pathlib.Path(tempfile.mkdtemp()) / "src"
        src.mkdir()
        (src / "a.py").write_text("import definitely_not_packaged_xyz\n", encoding="utf-8")
        r = bcc.check(self.APP, product_dir=src)
        self.assertIn("definitely_not_packaged_xyz", r["missing"],
                      "闭包缺失被判空 = 这道核验退化成摆设")
        self.assertGreaterEqual(r["checked"], 1)

    def test_动态import点要被列成人工确认项(self):
        src = pathlib.Path(tempfile.mkdtemp()) / "src"
        src.mkdir()
        (src / "b.py").write_text("import importlib\ndef f(n):\n    return importlib.import_module(n)\n",
                                  encoding="utf-8")
        _tops, dynamic = bcc.collect_imports(src)
        self.assertTrue(any("运行时算出来" in d for d in dynamic),
                        "hiddenimports 的来源就是这些点，漏列等于把 v1.3.74 那一幕留着")
        self.assertIn("importlib", _tops, "importlib 本身也该进顶层名")


if __name__ == "__main__":
    unittest.main(verbosity=2)
