#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本机状态前提核验（第一道防线，2026-10-22 起）—— 回答「还有几处用例把本机瞬时状态当隐式前提」。

要治的那件事
------------
2026-10-21 实测：默认序整档 26 条红，代码一字未改，红的原因是**那台机器的屏幕当时锁着**
（`sf_presence.screen_unavailable()` 返回 True，人脸守卫的守卫每拍提前 return，玻璃那两条的
真合成窗口根本不上屏）。修掉那两枚之后留下一个清单型问题：**这类形状还有几处？**
"我记得没有了"答不了这个问题，所以把它做成扫描。

只收一类，故意不收的三类同样重要
--------------------------------
判据只盯一种形状：**用例里真实调用了"系统对本机瞬时状态的回答"，而同一个函数体里没有
桩、没有带理由的跳过、没有申报标记**。宽一格就没人信它了，所以明确**不算违规**的三种：

1. **探针本身就是被测对象**（`tests/test_update_helper.py` 验助手对临时根的判断、
   `tests/test_v1423_temp_sweep.py` 扫进程表）—— 那些不是"前提"，是"题目"；
2. **值是被显式构造的**（`mock.patch.object(sf_presence, "screen_unavailable", lambda: True)`、
   `_FakeNSWindow(on_active_space=False)`、`main.isOnActiveSpace.return_value = True`）——
   用例自己决定前提，换台机器结论不变；
3. **只在源码文本里出现那个名字**（`self.assertIn("CGSGetActiveSpace", src)`）——
   那是 §6 第 2 条那类字符串断言，形状不同、危害不同，不许混在这一把尺子里报。
   （这一条是本轮实测出来的：第一版按"文件里出现过探针名"统计报了 30 处，逐条看下去
   30 处里 27 处是上面三种正当用法 —— 一把报假警的尺子会被下一轮拿去开豁免。）

申报标记：确实要读真实值（例如取证类用例），在**那一行末尾**写
`# machine-state-ok: 为什么必须读真实值`，理由为空即判违规。

形状规矩（AGENTS §3.1「链上工具的参数形状规矩」）
------------------------------------------------
- `--self-test` 里种五处错，五处都必须被抓到；
- 未知参数退 2，绝不落到默认动作；
- 扫到的测试文件少于 `MIN_TEST_FILES` → **拒绝下结论退 2**（`glob` 对空目录和不存在的
  目录都安静返回空表，"零输入 = 整关通过"是台账 G24 / `test_discipline_scan.min_files` 那一族）。

用法
----
    ./.venv/bin/python tools/machine_state_scan.py [--report|--self-test] [--dir <目录>]
"""
from __future__ import annotations

import argparse
import ast
import pathlib
import re
import sys

MIN_TEST_FILES = 60          # 少于这个数＝扫描范围失效，拒答（本仓 tests/ 实测远大于它）

# 「系统对本机瞬时状态的回答」：取值随屏幕电源、热量、电池、Space、锁屏状态变。
PROBES = {
    "屏幕锁定/显示器休眠": [r"\bscreen_unavailable\b", r"\bCGSSessionScreenIsLocked\b",
                       r"\bCGDisplayIsAsleep\b", r"\bCGDisplayIsAsleep\b"],
    "电源/热量/电池": [r"\bthermalState\b", r"\bisLowPowerModeEnabled\b",
                  r"\bpowerState\b", r"\bbatteryInformationList\b"],
    "活动 Space 与合成": [r"\bCGSGetActiveSpace\b", r"\bisOnActiveSpace\b",
                     r"\bCGWindowListCopyWindowInfo\b", r"\bwatch_active_space\b"],
}
# 体内出现这些就说明前提是被显式控制的（桩 / 假对象 / 带理由跳过 / 申报标记）
GUARDS = re.compile(
    r"patch\.object|patch\.dict|patch\.attribute|mock\.patch|MagicMock|Mock\("
    r"|SkipTest|skipIf|skipUnless|unittest\.skip|_Fake[A-Za-z]*\(|return_value\s*="
)
DECL = re.compile(r"#\s*machine-state-ok\s*:\s*(\S.*)$")

FAMILY_FOR = {p: fam for fam, pats in PROBES.items() for p in pats}


def _probe_calls(tree: ast.AST, source: str):
    """返回 (探针名, 调用所在函数名, 行号)：只算**真调用**，纯字符串出现不算。"""
    out = []
    lines = source.splitlines()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        seg = ast.get_source_segment(source, node.func) or ""
        hit = next((p for pats in PROBES.values() for p in pats if re.search(p, seg)), None)
        if hit is None:
            continue
        owner = ""
        for fn in [n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name.startswith("test")]:
            if fn.lineno <= node.lineno <= (fn.end_lineno or fn.lineno):
                owner = fn.name
                break
        out.append((FAMILY_FOR[hit], owner, node.lineno, seg))
    return out


def _body_has_guard(source: str, tree: ast.AST, fname: str, lineno: int) -> bool:
    """同一个函数体里有没有桩/假对象/带理由跳过；申报标记只看那一行。"""
    for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        if fn.name == fname and fn.lineno <= lineno <= (fn.end_lineno or fn.lineno):
            body = ast.get_source_segment(source, fn) or ""
            if GUARDS.search(body):
                return True
    line = source.splitlines()[lineno - 1] if lineno <= len(source.splitlines()) else ""
    m = DECL.search(line)
    return bool(m and m.group(1).strip())


def scan(root: pathlib.Path):
    """扫描 tests/ 下全部用例文件，返回 (违规清单, 扫到的文件数)。"""
    files = sorted((root / "tests").glob("test_*.py"))
    bad = []
    for p in files:
        try:
            source = p.read_text(encoding="utf-8")
            tree = ast.parse(source)
        except (SyntaxError, UnicodeDecodeError):
            bad.append((p.name, "-", 0, "解析失败（尺子不许把读不动当干净）"))
            continue
        for fam, fname, lineno, seg in _probe_calls(tree, source):
            if not fname:
                continue          # 模块级或辅助函数：不是"用例的前提"，交给模块级约定
            if _body_has_guard(source, tree, fname, lineno):
                continue
            bad.append((p.name, fname, lineno, f"{fam} 直调 {seg}"))
    return bad, len(files)


def report(root: pathlib.Path) -> int:
    bad, n = scan(root)
    if n < MIN_TEST_FILES:
        print(f"!! 只扫到 {n} 个用例文件（下限 {MIN_TEST_FILES}）—— 范围失效，拒绝下结论", file=sys.stderr)
        return 2
    if bad:
        print(f"== 本机状态前提核验：{n} 个文件里 {len(bad)} 处未防护 ==")
        for f, fn, ln, what in bad:
            print(f"   {f}:{ln}  {fn}()  {what}")
        print("   处理：桩掉那个取值、或改成带理由的 skip、或在那一行写 "
              "`# machine-state-ok: 为什么必须读真实值`")
        return 1
    print(f"== 本机状态前提核验通过：{n} 个用例文件，0 处把本机瞬时状态当隐式前提 ==")
    return 0


def _samples() -> dict[str, tuple[str, int]]:
    """五处种错：四处必须判违规、一处必须判干净（正向对照，防尺子过宽）。"""
    a = '''
import sf_presence
class T:
    def test_reads_screen_state(self):
        self.assertTrue(sf_presence.screen_unavailable())
'''
    b = '''
import sf_presence
from unittest import mock
class T:
    def test_stubbed_ok(self):
        with mock.patch.object(sf_presence, "screen_unavailable", lambda: False):
            pass
    def test_reads_screen_state(self):
        self.assertTrue(sf_presence.screen_unavailable())
'''
    c = '''
import sf_presence
class T:
    def test_declared_without_reason(self):
        sf_presence.screen_unavailable()  # machine-state-ok:
'''
    d = '''
class T:
    def test_only_a_string(self):
        src = "CGSGetActiveSpace"
        self.assertIn("CGSGetActiveSpace", src)
'''
    e = '''
import sf_glass
class T:
    def test_real_compositor_call(self):
        self.assertTrue(sf_glass.CGSGetActiveSpace())
'''
    return {"探针直调无桩": (a, 1), "桩在另一个用例里不算": (b, 1),
            "申报标记没写理由": (c, 1), "字符串里出现探针名不算违规": (d, 0),
            "假对象造前提不算违规": ('''
class _FakeWin:
    def __init__(self, on_active_space): self._v = on_active_space
    def isOnActiveSpace(self): return self._v
class T:
    def test_fake_driven(self):
        w = _FakeWin(on_active_space=False)
        self.assertTrue(w.isOnActiveSpace())
''', 0),
            "真调用活动 Space": (e, 1)}


def self_test() -> int:
    import tempfile
    root = pathlib.Path(tempfile.mkdtemp(prefix="sf_machine_state_scan_"))
    (root / "tests").mkdir(parents=True, exist_ok=True)
    fails = []
    for i in range(MIN_TEST_FILES):        # 先把范围红线喂饱，免得每种样例都红在范围上
        (root / "tests" / f"test filler_{i}.py").write_text("def x():\n    pass\n", encoding="utf-8")
    for name, (src, expected) in _samples().items():
        p = root / "tests" / f"test_zz_{abs(hash(name))}.py"
        p.write_text(src, encoding="utf-8")
        bad, n = scan(root)
        got = 1 if any(b[0] == p.name and b[3] != "解析失败（尺子不许把读不动当干净）" for b in bad) else 0
        if expected == 1 and got != 1:
            fails.append(f"{name}：种了错却没抓到")
        if expected == 0 and got == 1:
            fails.append(f"{name}：正当写法被判违规（尺子过宽）")
        p.unlink()
    # 范围红线：只放三个文件必须拒答
    small = pathlib.Path(tempfile.mkdtemp(prefix="sf_mss_small_"))
    (small / "tests").mkdir(parents=True)
    for i in range(3):
        (small / "tests" / f"test_{i}.py").write_text("def x():\n    pass\n", encoding="utf-8")
    if report(small) != 2:
        fails.append("范围红线：三个文件时没有拒绝下结论")
    if fails:
        print("!! 自检失败：", "；".join(fails), file=sys.stderr)
        return 1
    print(f"== 自检通过：{len(_samples())} 种形状 + 1 条范围红线，全部按预期（含两正当写法不误报）==")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="本机状态前提核验（只读）")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--self-test", dest="self_test", action="store_true")
    ap.add_argument("--dir", default=None, help="替代仓库根（自检用）")
    ns = ap.parse_args(argv)
    if ns.report and ns.self_test:
        print("!! --report 与 --self-test 互斥", file=sys.stderr)
        return 2
    if ns.self_test:
        return self_test()
    root = pathlib.Path(ns.dir) if ns.dir else pathlib.Path(__file__).resolve().parent.parent
    return report(root)


if __name__ == "__main__":
    sys.exit(main())
