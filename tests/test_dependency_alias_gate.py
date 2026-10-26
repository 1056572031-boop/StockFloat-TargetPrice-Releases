# -*- coding: utf-8 -*-
"""依赖声明的可查性判据：`tools/deadcode_scan.py` 的"requirements 疑似未使用"不许说谎。

为什么需要这个文件（2026-10-13 清理批）
--------------------------------------
那行输出曾经稳定地报 `pyobjc-framework-CoreMedia` 与 `pyobjc-framework-CoreAudio`
"疑似未使用"，而 AGENTS §1 与 `StockFloat.spec` 都写明这两个不能省（人脸守卫要的
ObjC block 方法元数据由它们提供；缺了授权框弹不出来，三条"不加依赖"的绕法实测都不通）。
照那行结论去删 requirements.txt，回归全绿、装机版坏 —— 正是"闸门说假话"那一族。

根因不在缺两个别名，而在**判定证据的形状**：它拿"候选名在 .py 语料或 spec 里出现过没有"
当"是否被使用"，于是"有人在注释里写过发行包名"就能让一个包算已用，而只在 spec 的
hiddenimports 里以裸模块名出现的包反被报成未使用。AVFoundation 侥幸过关、另两个每次被点名，
差的就是这一点运气。

本文件守的事，全部在临时树里跑，不依赖本机仓库内容：
  1. 真仓库现在必须**一条"疑似未使用"都报不出来**（每条依赖都有可查落点）；
  2. 真未使用的依赖必须仍被抓到 —— 否则第 1 条可能只是函数坏了返回空表（同
     `test_discipline_scan.min_files` / `coverage_gate` 的"空输入不许算通过"那一族）；
  3. 救住一个框架的是**模块名在 spec 里有落点**，不是注释里提过包名（配对树：
     spec 有裸模块名 → 不报；spec 没有 → 报）；
  4. 新增一枚 pyobjc 框架却没声明模块名落点 → 必须报（把"下一个 CoreMedia"提前拦住）。

还有一条**写在案上的已知局限**（种错实测抓出来的，见 test_metadata_frameworks_declared_in_spec_hiddenimports）：
第 1/2 条依赖的 `requirement_deps()` 语料**含 tests/**，所以本文件把这些包名当字面量写进去，
就已经让它们"看起来被使用了" —— 今天摘掉三条别名，只有第 3/4 条红，第 1/2 条照样绿。
因此真正硬的锁是第 3、4、5 条（不看不含语料的判据、以及 spec 的 hiddenimports），
第 1/2 条只当"回归时至少有人来看一眼"的哨兵。
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import shutil
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
for _p in (str(ROOT), str(ROOT / "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from _base import SFCase              # noqa: E402
except ImportError:
    from tests._base import SFCase        # noqa: E402

# 三个"只提供桥接元数据、代码里从不 import"的框架（AGENTS §1 / spec hiddenimports）
METADATA_ONLY_FRAMEWORKS = (
    "pyobjc-framework-AVFoundation",
    "pyobjc-framework-CoreMedia",
    "pyobjc-framework-CoreAudio",
)


def _load_deadcode_scan():
    """tools/ 不是包，按文件路径加载（与 tests/test_reexport_contract.py 同一姿势）。"""
    spec = importlib.util.spec_from_file_location(
        "deadcode_scan", ROOT / "tools" / "deadcode_scan.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fake_tree(requirements_lines, spec_text):
    """造一棵最小假仓库：只有 requirements.txt 与 StockFloat.spec。

    刻意不给 tests/ 与 *.py —— `read()` 对缺失文件返回空串，语料为空正是想要的：
    这样"是否被使用"完全由 spec 里有没有那个模块名决定，注释侥幸那条路径被排除。
    """
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="sf-deps-"))
    (tmp / "requirements.txt").write_text("\n".join(requirements_lines) + "\n", encoding="utf-8")
    (tmp / "StockFloat.spec").write_text(spec_text, encoding="utf-8")
    return tmp


class TestDependencyClaimGate(SFCase):
    def setUp(self):
        super().setUp()
        self.ds = _load_deadcode_scan()
        self._orig_root = self.ds.ROOT
        self.addCleanup(setattr, self.ds, "ROOT", self._orig_root)
        self._tmps = []
        self.addCleanup(self._rm_tmps)

    def _rm_tmps(self):
        for p in self._tmps:
            shutil.rmtree(p, ignore_errors=True)

    def _run_with(self, tmp):
        self.ds.ROOT = tmp
        return self.ds.requirement_deps()

    # 1) 真仓库：一条谎报都不许有
    def test_real_repo_reports_no_unused_dependency(self):
        self.ds.ROOT = ROOT          # 显式恢复，防上一条用例把 ROOT 留在临时树
        phantom = self.ds.requirement_deps()
        self.assertEqual(
            phantom, [],
            "requirements.txt 里有依赖被报成\"全仓从未 import\"：%s —— 要么它真的没人用（该删），"
            "要么它的落点没声明（该在 alias 里补模块名，或在 spec 的 hiddenimports 里登记）。"
            "无论哪种，都不许把这行输出当'可以直接删'的依据。" % phantom)

    def test_metadata_only_frameworks_are_never_suspected(self):
        """点名那三个：它们曾被逐版谎报，是最容易被再次弄坏的一族。"""
        self.ds.ROOT = ROOT
        phantom = self.ds.requirement_deps()
        for name in METADATA_ONLY_FRAMEWORKS:
            with self.subTest(dep=name):
                self.assertNotIn(name, phantom,
                                 "%s 又被报成未使用 —— 它提供 ObjC block 方法元数据，"
                                 "删了装机版摄像头授权框就弹不出来（AGENTS §1）" % name)

    # 2) 非空洞性：真未使用的必须仍被抓到
    def test_genuinely_unused_dep_is_still_caught(self):
        tmp = _fake_tree(["totally-not-a-package==1.2.3"], "# spec without it\n")
        self._tmps.append(tmp)
        self.assertIn("totally-not-a-package", self._run_with(tmp),
                      "假树里一枚谁都没用的依赖都没被抓到 —— requirement_deps() 恒空，"
                      "上面那条'真仓库报不出来'就成了永远通过的假门")

    # 3) 配对树：救住它的是"模块名在 spec 里有落点"
    def test_module_name_landing_in_spec_is_what_saves_a_framework(self):
        with_landing = _fake_tree(["pyobjc-framework-CoreMedia==12.2.2"],
                                  "hiddenimports=['CoreMedia'],\n")
        without_landing = _fake_tree(["pyobjc-framework-CoreMedia==12.2.2"],
                                     "hiddenimports=['SomethingElse'],\n")
        self._tmps += [with_landing, without_landing]
        self.assertEqual(self._run_with(with_landing), [],
                         "spec 的 hiddenimports 写了裸模块名 CoreMedia 却仍被判未使用 —— "
                         "alias 的模块名映射失效了")
        self.assertIn("pyobjc-framework-CoreMedia", self._run_with(without_landing),
                      "spec 里没有该模块名时竟然不算可疑 —— 判据已经退化成'包名在任意文本里"
                      "出现过就行'，那正是本文件要拆掉的那台假门")

    def test_setuptools_is_only_accepted_with_a_pyinstaller_build_landing(self):
        with_builder = _fake_tree(["setuptools==84.1.0"], "from PyInstaller import __main__\n")
        without_builder = _fake_tree(["setuptools==84.1.0"], "# no build tool\n")
        self._tmps += [with_builder, without_builder]
        self.assertEqual(self._run_with(with_builder), [],
                         "PyInstaller 的构建时 setuptools 依赖被误报未使用")
        self.assertIn("setuptools", self._run_with(without_builder),
                      "没有 PyInstaller 落点的 setuptools 竟未被报出，构建依赖别名过宽")

    # 4) 前瞻锁：新增框架必须有落点声明
    def test_new_framework_without_landing_is_flagged(self):
        tmp = _fake_tree(["pyobjc-framework-BrandNew==12.2.2"],
                         "hiddenimports=['AVFoundation'],\n")
        self._tmps.append(tmp)
        self.assertIn("pyobjc-framework-BrandNew", self._run_with(tmp),
                      "新增一枚 pyobjc 框架、代码与 spec 都没有它的模块名，却没被抓到 —— "
                      "'下一个 CoreMedia'又会以'疑似未使用'的名义被删掉")

    # 5) 上面 1/2 两条有个实测出来的洞，这里补一条不受它影响的锁
    def _hidden_imports_block(self, spec_text):
        """取 spec 里 hiddenimports=[...] 那一段（跨行）。"""
        import re

        m = re.search(r"hiddenimports=\[(.*?)\]", spec_text, re.S)
        return m.group(1) if m else ""

    def test_hidden_imports_extractor_is_not_blind(self):
        """提取器必须在"没有 hiddenimports"的样本上返回空 —— 否则下一条是空断言。"""
        self.assertEqual(self._hidden_imports_block("a=1\n"), "",
                         "没有 hiddenimports 段却返回了非空串，提取逻辑不可信")
        self.assertIn("CoreMedia", self._hidden_imports_block("hiddenimports=['CoreMedia'],\n"))

    def test_metadata_frameworks_declared_in_spec_hiddenimports(self):
        """真正的不变量是"打包时这三个框架要显式登记"，而不是"包名在哪个文件里出现过"。

        为什么单独立一条（2026-10-13 种错实测抓出来的洞）：`requirement_deps()` 的语料**含
        tests/**，所以任何一条测试只要把这些发行包名当字面量写进去，就等于自己把"疑似未使用"
        抹平了 —— 今天摘掉三条别名，第 1/2 条照样绿，只有配对树那两条红。判据把自己写进了
        被测证据，这正是本文件要拆的那台假门的形状，不能不留痕迹地放过。
        所以这条不看语料、只看 spec：惰性 import 的框架一旦被从 hiddenimports 摘掉，
        PyInstaller 静态分析看不见它，装机版第一次用就炸（AGENTS §1 的 TypeError 事故）。
        """
        spec = (ROOT / "StockFloat.spec").read_text(encoding="utf-8")
        block = self._hidden_imports_block(spec)
        self.assertTrue(block, "StockFloat.spec 里取不到 hiddenimports 段 —— 提取失效，不许算通过")
        for mod in ("AVFoundation", "CoreMedia", "CoreAudio"):
            with self.subTest(module=mod):
                self.assertIn(mod, block,
                              "%s 不在 spec 的 hiddenimports 里：sf_presence 是函数内惰性 import，"
                              "静态分析看不见，产物会缺包（AGENTS §1）" % mod)


if __name__ == "__main__":
    unittest.main()
