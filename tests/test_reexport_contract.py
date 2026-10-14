# -*- coding: utf-8 -*-
"""「再导出契约」守护用例（v1.3.46 瘦身期建立）。

为什么需要这个文件
------------------
stock_float_window.py 顶部有 9 个符号，在本文件正文里**一次都没用过**（2026-10-13 口径，
`fmt_pct` 已在清理批里正式并进白名单，此前它是漏登记的第 9 个）：

    RED / GREEN / FG_DIM            —— 涨跌/中性色（sf_ui_render、sf_constants 再导出）
    _migrate_legacy_config          —— 旧路径配置迁移（sf_config 再导出）
    _drop_flash_due                 —— 下跌闪烁判定（sf_utils 再导出）
    POLL_DEGRADED /
    POLL_INTERVAL_DEGRADED /
    POLL_INTERVAL_NORMAL            —— 轮询状态与间隔常量（sf_constants 再导出）

它们看起来就是典型的"没用的 import"，任何一次瘦身都会先拿它们开刀。但实际上它们是
**测试契约**：tests/ 下多处用 `m.符号` 直接访问（因为生产代码也正是从
stock_float_window 这个聚合模块取这些名字），例如：

    test_render.py       assert_solid_block_found(img, self._muted(m.RED), ...)
    test_core.py:449     self.assertTrue(m._drop_flash_due(-4.0, 4.0, None, 100.0, 10.0))
    test_migrate.py:56   m._migrate_legacy_config()
    test_v165.py:640     self.assertEqual(self.win._poll_state, m.POLL_DEGRADED)
    test_v123.py:161     self.assertEqual(self.win._poll_interval(), m.POLL_INTERVAL_NORMAL)

一旦从 stock_float_window.py 删掉这些 import，上面这些用例会立刻 AttributeError。
v1.3.46 瘦身时差点误删，因此固化成本用例 + tools/deadcode_scan.py 的显式白名单。

本文件守三件事：
  1. 9 个符号在 stock_float_window 模块上仍然都存在（少一个就红）；
  2. deadcode_scan.REEXPORT_CONTRACT 与这 9 个符号**完全一致**（防两边漂移：
     模块删了白名单没删、或白名单写了模块里根本没有的名字）；
  3. 扫描器对这 9 个符号给出 RISKY + "再导出契约" 结论，且能正常跑完 ——
     即白名单真的生效，而不是靠别的规则碰巧命中。

第 4 件是清理批补上的（见 TestUndeclaredReexports）：1/2/3 只比白名单与它自己那份集合，
回答不了"还有第几个直访名字漏在清单外"。当时实测漏了一个（`fmt_pct`），于是先加一条锁形状的
判据 —— 顶层引入而正文未用的名字必须全部登记在册，漏一个红、登记过期也红；
后来那个名字已正式并进白名单，而**这条判据留着**：它守的是下一个漏项，不是那一个。
"""
import contextlib
import importlib.util
import io
import json
import os
import pathlib
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import stock_float_window as m  # noqa: E402  （需要先 sys.path 才能导入）

try:
    from _base import SFCase              # noqa: E402
except ImportError:
    from tests._base import SFCase        # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent

# 契约的"真值"刻意在本文件里再抄一份：它与 deadcode_scan.REEXPORT_CONTRACT 互为
# 交叉校验，任何一边单方面改动都会让 test_whitelist_matches_contract 变红。
REEXPORT_CONTRACT = frozenset({
    "RED",
    "GREEN",
    "FG_DIM",
    "_migrate_legacy_config",
    "_drop_flash_due",
    "fmt_pct",
    "POLL_DEGRADED",
    "POLL_INTERVAL_DEGRADED",
    "POLL_INTERVAL_NORMAL",
})


def _load_deadcode_scan():
    """tools/ 不是包，按文件路径加载（与 tests/test_verify_bundle_tools.py 一致）。"""
    spec = importlib.util.spec_from_file_location(
        "deadcode_scan", ROOT / "tools" / "deadcode_scan.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_static_gate():
    """同法加载 tools/static_gate.py —— 用它的公开判据 run_ruff_ratchet()。

    不复算 F401、也不 copy 它的 ruff 调用参数：闸门用哪把尺子，这条判据就用同一把，
    否则"测试绿、闸门红"这类对不上的账又得靠人来查（AGENTS §1 同一族）。
    """
    spec = importlib.util.spec_from_file_location(
        "static_gate", ROOT / "tools" / "static_gate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# run_ruff_ratchet() 扫全部产品根模块，一次约 1 秒；本类三个用例问的是同一件事，
# 所以进程内缓存一份。用例之间不改源码，缓存不会串味。
_RUFF_F401_CACHE: dict = {}


class TestReexportContractSymbols(unittest.TestCase):
    """1) 9 个符号必须在 stock_float_window 模块对象上可取。"""

    def test_all_contract_symbols_exist_on_module(self):
        missing = sorted(n for n in REEXPORT_CONTRACT if not hasattr(m, n))
        self.assertEqual(missing, [],
                         "stock_float_window 缺少再导出契约符号（测试以 m.符号 直访，"
                         "删掉会立刻 AttributeError）：%s" % ", ".join(missing))

    def test_each_symbol_is_reachable_by_getattr(self):
        # 逐符号断言，失败时能一眼看出少了哪一个
        for name in sorted(REEXPORT_CONTRACT):
            with self.subTest(symbol=name):
                self.assertIsNotNone(getattr(m, name, None),
                                     "m.%s 不存在，再导出契约被破坏" % name)


class TestWhitelistConsistency(unittest.TestCase):
    """2) 扫描器白名单与契约集合必须完全一致，防止两边漂移。"""

    def test_whitelist_matches_contract(self):
        ds = _load_deadcode_scan()
        self.assertEqual(set(ds.REEXPORT_CONTRACT), set(REEXPORT_CONTRACT),
                         "deadcode_scan.REEXPORT_CONTRACT 与契约集合不一致："
                         "白名单多出 %s / 缺少 %s（改动符号须同步两边）"
                         % (sorted(set(ds.REEXPORT_CONTRACT) - set(REEXPORT_CONTRACT)),
                            sorted(set(REEXPORT_CONTRACT) - set(ds.REEXPORT_CONTRACT))))

    def test_whitelist_names_all_exist_on_module(self):
        ds = _load_deadcode_scan()
        ghost = sorted(n for n in ds.REEXPORT_CONTRACT if not hasattr(m, n))
        self.assertEqual(ghost, [],
                         "白名单里的名字在 stock_float_window 上已不存在：%s" % ", ".join(ghost))


class TestScannerHonoursWhitelist(unittest.TestCase):
    """3) 扫描器对这 9 个符号给出"再导出契约"结论，而不是笼统的"疑似"。"""

    def test_classify_returns_reexport_reason(self):
        ds = _load_deadcode_scan()
        corpus = ds.Corpus(ds.ref_files())
        item = {"kind": "unused_import", "name": "-", "line": 1}
        sf_path = ROOT / "stock_float_window.py"
        for name in sorted(REEXPORT_CONTRACT):
            with self.subTest(symbol=name):
                level, why = ds.classify(sf_path, dict(item, name=name), corpus)
                self.assertEqual(level, "RISKY", "%s 被判成 %s，白名单未生效" % (name, level))
                self.assertIn("再导出契约", why,
                              "%s 的判定理由必须写明是再导出契约，实际：%s" % (name, why))

    def test_full_scan_reports_contract_symbols_as_reexport(self):
        """端到端跑一遍扫描器：能跑完，且报告里出现的契约符号一律是"再导出契约"。"""
        ds = _load_deadcode_scan()
        buf = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp) / "deadcode.json"
            argv = sys.argv[:]
            sys.argv = ["deadcode_scan.py", "--json", str(out)]
            try:
                with contextlib.redirect_stdout(buf):
                    rc = ds.main()
            finally:
                sys.argv = argv
            self.assertEqual(rc, 0)
            report = json.loads(out.read_text(encoding="utf-8"))

        text = buf.getvalue()
        self.assertIn("== 瘦身扫描", text, "扫描器未能正常输出报告")
        self.assertIn("SAFE", text)

        # 只断言"扫到的契约符号一律是再导出契约"，**不**锁死 8 条：
        # 若将来某个符号在 stock_float_window 正文里被真正使用，它就不再产生
        # unused_import 条目（属于正常演化）。全量结论由上面的
        # test_classify_returns_reexport_reason 无条件覆盖，这里只守"白名单真生效"。
        hits = 0
        for f in report["files"]:
            for it in f.get("items", []):
                if it.get("name") in REEXPORT_CONTRACT:
                    hits += 1
                    self.assertEqual(it["level"], "RISKY",
                                     "%s 被判成 SAFE（可删），再导出契约失守" % it["name"])
                    self.assertIn("再导出契约", it["why"])
        self.assertGreater(hits, 0, "扫描报告里一个契约符号都没有，白名单形同虚设")


class TestUndeclaredReexports(SFCase):
    """4) 白名单之外还有没有"看着没用、其实被测试按名直访"的名字。

    上面三个类只比"白名单 ↔ 扫描器认的那份"，两边一致也回答不了那个更要紧的问题：
    **还有第几个名字漏在清单外**。2026-10-12 实测到一处，三条都是当场取证的：
      · `stock_float_window.py:109 from sf_utils import fmt_pct` 被 ruff 报 F401（顶层那条没被用）；
        正文用到的是 `:3736` 方法内的第二条同名 import —— 也就是说 F401 的判定是对的；
      · `tools/deadcode_scan.py` **当时**对它既没给 SAFE 也没给 RISKY —— 白名单里没有它，
        扫描报告里连条目都没有，等于没有任何东西在守它（2026-10-13 清理批已把它并进白名单，
        于是这一条从"漏洞"变成"有据可查的历史"）；
      · `tests/test_core.py:34-41` 连用 6 次 `m.fmt_pct(...)`（m = stock_float_window），
        谁按"ruff 说未用"删掉顶层那条，这 6 处当场 AttributeError。

    发现它的那天 tools/ 在只读区，所以先在 tests 侧锁形状；本轮解禁后把名字并进了白名单。
    **两条都留着**：白名单管"这一条别再漏"，形状锁管"下一条漏了立刻红"——取 ruff 报为未用的
    顶层 import 名字，要求每一个都落在名单内。
    """

    # 过渡登记处：本轮（2026-10-13）已把 fmt_pct 正式并进 tools 的白名单，这里清空。
    # 保留这个集合与下面三条判据，是因为它们守的对象不是 fmt_pct，而是**下一个漏项**：
    # 只要有人新增一条"正文没用、测试按 m.名字 直访"的顶层 import 而没进白名单，
    # test_every_undeclared_import_is_known 就当场红。
    UNDECLARED_REEXPORTS = frozenset()

    def _unused_imports_by_ruff(self):
        """问 ruff 要未用导入 —— 走闸门自己的公开判据，不复算也不用它的私有函数。

        为什么不用 ast.walk 手搓：`fmt_pct` 在 stock_float_window 里出现两次（顶层 109 行 +
        方法内 3736 行重复 import 并在 3739 使用）。裸 ast.walk 不区分作用域，会把顶层那条
        判成"正文用上了"，于是这条判据永远看不见它 —— 判据自己说假话比没有更坏（AGENTS §5.1）。
        而 F401 本来就是这套账的尺子：`tools/static_gate.py` 的 B 级棘轮用的就是它，
        沿用自己的公开函数 `run_ruff_ratchet()` 等于保证"测试里的尺子"和"闸门里的尺子"
        永远同一把，也不会因为碰私有名 `_ruff_exe()` 多欠一条 §6 第 5 条。
        """
        cached = _RUFF_F401_CACHE.get("names")
        if cached is not None:
            return cached
        sg = _load_static_gate()
        _counts, raw = sg.run_ruff_ratchet()      # 非 0/1 退出码它自己 raise，不会静默归零
        found = set()
        for line in raw.splitlines():
            if "F401" not in line or "stock_float_window.py" not in line:
                continue
            hit = re.search(r"`([^`]+)` imported but unused", line)
            if hit:
                found.add(hit.group(1).split(".")[-1])
        _RUFF_F401_CACHE["names"] = found
        return found

    def test_detector_is_not_blind(self):
        """正向对照：必须真的检出白名单那 8 个，否则"零未登记"是空输入假绿。

        与 test_discipline_scan 的 min_files=100 同一族 —— 取到空表时"全部在册"
        会无条件成立，判据形同虚设。
        """
        found = self._unused_imports_by_ruff()
        self.assertTrue(found, "ruff 未报出任何未用导入 —— 尺子或调用方式失效，下面的判定不可信")
        missing = sorted(REEXPORT_CONTRACT - found)
        self.assertEqual(missing, [],
                         "ruff 这次没报出白名单符号 %s（作用域分析或调用参数变了），"
                         "这条判据的地基塌了" % missing)

    def test_every_undeclared_import_is_known(self):
        undeclared = sorted(self._unused_imports_by_ruff()
                            - set(REEXPORT_CONTRACT) - self.UNDECLARED_REEXPORTS)
        self.assertEqual(undeclared, [],
                         "顶层 import 未使用又没有登记为再导出的名字：%s —— 要么它是真垃圾可删，"
                         "要么它被测试按名直访而清单漏了它。先查 tests/ 里的 m.名字 用法再决定，"
                         "不许直接删" % undeclared)

    def test_undeclared_list_is_not_stale(self):
        """登记的未白名单再导出必须仍处于"ruff 报未用"的状态。"""
        ghost = sorted(self.UNDECLARED_REEXPORTS - self._unused_imports_by_ruff())
        self.assertEqual(ghost, [],
                         "UNDECLARED_REEXPORTS 里的 %s 已不再被 ruff 报成未用导入（正文真用上了、"
                         "或已被移除），这条登记要一起收掉" % ghost)

    def test_fmt_pct_reachable_and_live_on_aggregator(self):
        """按名直访的实位证明：m.fmt_pct 存在、可调用、与 sf_utils 那份是同一个实现。"""
        import sf_utils

        self.assertTrue(hasattr(m, "fmt_pct"),
                        "stock_float_window.fmt_pct 不见了 —— test_core 里 6 处 m.fmt_pct 会红")
        self.assertIs(m.fmt_pct, sf_utils.fmt_pct,
                      "m.fmt_pct 不再是 sf_utils 的同一实现（再导出被换成了本地副本）")
        self.assertEqual(m.fmt_pct(0.50), "+0.50%")


if __name__ == "__main__":
    unittest.main()
