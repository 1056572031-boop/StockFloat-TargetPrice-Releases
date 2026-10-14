# -*- coding: utf-8 -*-
"""`tools/coverage_gate.py` 的行为判据（v1.4.62 起，覆盖率分档）。

为什么单开一个文件，而不是只靠 `tests/test_release_gates.py` 那条链上扫描：
那条扫描是**起子进程**跑 `--self-test` 的，而子进程不计入 `.coverage`
（AGENTS §6 第 10 条）。也就是说新工具那 300 行如果只由子进程测，它一进仓
就把链上档拖低一截，看起来像"新增了没人核验的代码"——恰好是要拦的形状。
这里在主进程直调同一批函数：既是判据，也把这块账记回主进程。

守的四件事，按重要性排：
  1. **没有数据不算通过**。空 `.coverage`、文件不存在、coverage 跑不动，
     三种都必须给出非零码与人话理由；"零输入 = 整关绿"是本仓点名过的形状
     （`tools/test_discipline_scan.py` 的 `min_files` 红线同源）。
  2. **两档各判各的**。产品档很好、链上档掉下去，必须红 —— 否则分档白分。
  3. **下调下限必须显式开关**，且拒绝时不动基线文件。
  4. **自检自己必须过，而且必须只读**（v1.4.61 实测：一枚不认 `--self-test`
     的链上工具把真实对外安装文档重写了一遍，还返回 rc=0 报"自检通过"）。
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import coverage_gate as CG  # noqa: E402

try:
    from _base import SFCase                     # noqa: E402
except ImportError:                              # 以包形式被 discover 导入时
    from tests._base import SFCase               # noqa: E402

BASELINE_BYTES = CG.BASELINE.read_bytes() if CG.BASELINE.is_file() else None


def _run(args, env=None):
    """真起子进程跑工具，返回 (rc, 合并输出)。参数形状必须按 subprocess 验，
    主进程调 main() 摸不到 `SystemExit` 之外的那层。"""
    proc = subprocess.run([sys.executable, str(ROOT / "tools" / "coverage_gate.py")] + args,
                          cwd=str(ROOT), env=env, text=True,
                          capture_output=True, check=False, timeout=180)
    return proc.returncode, proc.stdout + proc.stderr


class TestJudgeTiers(SFCase):
    """两档各判各的：这是把一条 84 的混合线换成两条线的全部理由。"""

    @staticmethod
    def _judge(product, chain, product_floor=84, chain_floor=63):
        return CG.judge({"product": list(product), "chain": list(chain)},
                        product_floor, chain_floor)

    def test_both_tiers_above_floor_pass(self):
        rc, lines = self._judge((1000, 100, 12), (200, 60, 6))
        self.assertEqual(rc, 0, "两档都达标不该红：%s" % lines)

    def test_product_tier_below_floor_fails(self):
        rc, lines = self._judge((1000, 300, 12), (200, 10, 6))
        self.assertEqual(rc, 1, "产品档 70% 低于 84% 必须红")
        self.assertTrue(any("产品代码" in l and l.startswith("!!") for l in lines),
                        "红的理由必须点名是哪一档：%s" % lines)

    def test_chain_tier_below_floor_fails_even_when_product_is_healthy(self):
        """这条是分档的核心：产品档很漂亮时，链上档掉下去也必须红。

        旧的一条合并线做不到这个区分，反过来也做不到（链上拖低的合并红会被
        当成产品缺口去补测）。
        """
        rc, lines = self._judge((1000, 1, 12), (200, 150, 6))
        self.assertEqual(rc, 1, "链上档 25% 低于下限必须红，哪怕产品档 99.9%")
        self.assertTrue(any("tools/" in l and l.startswith("!!") for l in lines),
                        "必须点名发版链那一档：%s" % lines)

    def test_value_exactly_at_floor_passes(self):
        rc, _ = self._judge((100, 16, 4), (100, 37, 4))
        self.assertEqual(rc, 0, "恰好等于下限不该判红（边界做成永远红会让闸门失去意义）")

    def test_just_below_floor_fails(self):
        rc, _ = self._judge((100, 17, 4), (100, 10, 4))
        self.assertEqual(rc, 1, "低于下限 0.1 个百分点也必须红（不许有容差）")

    def test_no_data_at_all_is_not_a_pass(self):
        """空数据必须红。这是本仓点名的形状：零输入 = 整关通过。"""
        rc, lines = self._judge((0, 0, 0), (0, 0, 0))
        self.assertEqual(rc, 1, "两档都没数据绝不能绿：%s" % lines)

    def test_only_one_tier_matched_is_a_failure(self):
        """有一档一个文件都没配上 = 分档口径失效（比如 tools/ 被挪进子目录）。"""
        rc, lines = self._judge((1000, 10, 12), (0, 0, 0))
        self.assertEqual(rc, 1, "链上一档空了不能当作没问题：%s" % lines)
        self.assertTrue(any("没配上" in l for l in lines),
                        "要说清是范围失效而不是覆盖率低：%s" % lines)

    def test_zero_statements_with_files_is_not_a_hundred_percent(self):
        rc, _ = self._judge((0, 0, 3), (100, 1, 3))
        self.assertEqual(rc, 1, "语句数为 0 却报 100% 就是说假话")

    def test_missing_tier_key_is_treated_as_empty_not_a_crash(self):
        """调用方给个缺键的字典（将来加第三档忘了同步）也必须红，而不是 KeyError 崩关卡。"""
        rc, lines = CG.judge({"product": [1000, 10, 12]}, 84, 63)
        self.assertEqual(rc, 1, "缺 chain 键必须当成「这一档没数据」判红：%s" % lines)
        self.assertTrue(any("tools/" in l for l in lines),
                        "理由必须点名是哪一档：%s" % lines)


class TestWriteBackGuard(SFCase):
    """下调下限必须显式开关（与 static_gate 的 write_baseline 同口径）。"""

    def test_drop_without_flag_is_rejected(self):
        ok, why = CG.decide_update(new_chain=63, old_chain=70, allow_drop=False)
        self.assertFalse(ok, "实测低于基线却不给开关，绝不能放行")
        self.assertIn("--allow-drop", why, "拒绝的理由要告诉人正确的做法")

    def test_drop_with_flag_passes_but_warns(self):
        ok, why = CG.decide_update(new_chain=63, old_chain=70, allow_drop=True)
        self.assertTrue(ok, "显式给了开关就该放行（可以绕，绕了必须看得见）")
        self.assertIn("WARNING", why, "下调必须大声警告")

    def test_raise_needs_no_flag(self):
        ok, _ = CG.decide_update(new_chain=75, old_chain=70, allow_drop=False)
        self.assertTrue(ok, "抬高基线是好事，不该要额外开关")


class TestMinOverrideDirection(SFCase):
    """`--min` 只能往上加严，不能往下压 —— 下限 89 之后这条不再是可选口径。

    形状与 §2 里"SF_SKIP_COVERAGE / SF_RANDOM_ORDER 不落关卡时间戳"同一族：
    一道环境变量如果能悄悄把刚收紧的棘轮按回去，那把锁就等于没上。
    """

    def _baseline(self):
        return {"product_floor": 89, "chain_floor": 78}

    def test_lower_than_baseline_is_refused(self):
        b = self._baseline()
        self.assertEqual(CG.min_override(b, "84"), 2, "把 89 压回 84 必须退 2")
        self.assertEqual(b["product_floor"], 89, "拒绝时一个值都不许改")

    def test_raising_is_accepted_and_applied(self):
        b = self._baseline()
        self.assertEqual(CG.min_override(b, "91"), 0)
        self.assertEqual(b["product_floor"], 91, "临时加严必须真的生效，否则这只开关是假的")

    def test_equal_to_baseline_is_accepted(self):
        b = self._baseline()
        self.assertEqual(CG.min_override(b, "89"), 0, "等于基线不算压低，不该拒")
        self.assertEqual(b["product_floor"], 89)

    def test_non_integer_still_exits_two(self):
        self.assertEqual(CG.min_override(self._baseline(), "abc"), 2)

    def test_cli_path_refuses_a_silent_lowering(self):
        """走命令行也必须拦在分档判定之前 —— 不许先跑一遍再打提示。"""
        rc, out = _run(["--min=1"])
        self.assertEqual(rc, 2)
        self.assertIn("只许抬高", out)
        self.assertNotIn("覆盖率分档门禁", out, "被拒的那一轮不该已经跑出分档结论")


class TestBaselineLoading(SFCase):
    def test_missing_baseline_raises(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(RuntimeError):
                CG.load_baseline(Path(td) / "根本不存在.json")

    def test_baseline_without_floors_raises(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "b.json"
            p.write_text(json.dumps({"schema": 1}), encoding="utf-8")
            with self.assertRaises(RuntimeError):
                CG.load_baseline(p)

    def test_baseline_with_non_positive_floor_raises(self):
        """floor 被写成 0 就等于闸门大开，必须当坏数据拒掉。"""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "b.json"
            p.write_text(json.dumps({"schema": 1, "product_floor": 0, "chain_floor": -5}),
                         encoding="utf-8")
            with self.assertRaises(RuntimeError):
                CG.load_baseline(p)

    def test_shipped_baseline_is_usable(self):
        data = CG.load_baseline()
        # 这把"下限的下限"必须写在代码里：2026-10-13 产品档从 84 抬到 89 时，
        # 这里若还留着 84，谁都能把基线改回 84 而全量照绿 —— 正是台账 G12 给
        # static_gate 记过的那条"只降不升只写在文档里，代码没有比较分支"。
        # 抬档要两处一起改（基线 + 这一行），降档则必须在这里先说明理由。
        self.assertGreaterEqual(data["product_floor"], 89,
                                "产品档下限不许低于 89（2026-10-13 负责人拍板；AGENTS §6 第 10 条的棘轮方向）")
        self.assertGreaterEqual(data["chain_floor"], 60,
                                "链上档下限异常小，基线八成被清空了：%r" % data)


class TestDataAvailability(SFCase):
    """没有 coverage 数据时的三种形状，都必须给出非零码，不许"空转通过"。"""

    def test_read_totals_raises_when_data_file_absent(self):
        with tempfile.TemporaryDirectory() as td:
            missing = str(Path(td) / "no_such_data")
            with self.assertRaises(RuntimeError):
                CG.read_totals(data_file=missing)

    def test_load_data_returns_reason_instead_of_empty_tiers(self):
        with tempfile.TemporaryDirectory() as td:
            original = CG.read_totals
            try:
                CG.read_totals = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("种错"))
                data, why = CG.load_data()
            finally:
                CG.read_totals = original
        self.assertIsNone(data, "取数失败必须返回 None，不能让调用方拿着空 tiers 判绿")
        self.assertIn("种错", why, "理由必须带上原始错误，否则没法分诊")

    def test_cli_exits_2_when_no_coverage_data(self):
        """真子进程验一次：数据文件不存在时门禁必须非零退出且不抛栈。

        为什么不主进程调 main()：这一条判的是"脚本被 run_tests.sh 调用时
        的退出码"，而那正是发版链读的东西。
        """
        with tempfile.TemporaryDirectory() as td:
            env = os.environ.copy()
            env["COVERAGE_FILE"] = str(Path(td) / "absent")
            rc, out = _run([], env=env)
        self.assertEqual(rc, 2, "无数据必须退 2（got %r）：\n%s" % (rc, out[-500:]))
        self.assertIn("不算通过", out, "得让人看出是没跑而不是跑过了：\n%s" % out[-400:])
        self.assertNotIn("Traceback", out, "抛栈说明异常路径没收口")


class TestSelfTestIsReal(SFCase):
    def test_self_test_passes_and_is_readonly(self):
        """直调自检：既要全过，又必须一个字节都不写（v1.4.61 那枚事故的形状）。"""
        buf = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(err):
            rc = CG.self_test()
        out = buf.getvalue()
        self.assertEqual(rc, 0, "自检没过就是有该被抓到的错没抓到：\n%s%s"
                         % (out, err.getvalue()))
        self.assertIn("0 项失败", out, "结论行必须给出失败数：\n%s" % out)
        self.assertNotIn("种错未被抓到", out, "有变异样本没被抓到：\n%s" % out)
        self.assertEqual(CG.BASELINE.read_bytes(), BASELINE_BYTES,
                         "自检写了基线 —— 它必须只读")

    def test_cli_self_test_exits_zero(self):
        rc, out = _run(["--self-test"])
        self.assertEqual(rc, 0, "发版链那条扫描按 rc==2 判「没有可用自检」，"
                                "非零也会被记成自检自己没过：\n%s" % out[-600:])

    def test_unknown_flag_exits_2_and_writes_nothing(self):
        """AGENTS §3.1 那条硬规矩：未知参数绝不许落到默认动作里。

        v1.4.61 实测：不认 `--self-test` 的工具按"不是 --print/--check 就是写文件"
        处理，一次"自检"把真实 `../.sf-opensource/docs/INSTALL.md` 重写了。
        这里断言的不只是退出码，还有"仓库里的文件一个都没变"。
        """
        before = {p: p.stat().st_mtime_ns for p in
                  (CG.BASELINE, ROOT / "release_notes.txt", ROOT / "run_tests.sh")}
        rc, out = _run(["--definitely-not-a-real-flag"])
        self.assertEqual(rc, 2, "未知参数必须退 2（got %r）：\n%s" % (rc, out[-400:]))
        self.assertIn("不认识参数", out, "要说清哪些参数可用：\n%s" % out[-300:])
        for p, mtime in before.items():
            if p.exists():
                self.assertEqual(p.stat().st_mtime_ns, mtime, "%s 被动过了" % p)


class TestCoverageRatchetDirection(SFCase):
    """棘轮方向：链上档只降不升，而"降"要靠补测而不是改数字。"""

    def test_baseline_measured_fields_are_not_the_gate(self):
        """判定只用 floor；measured_* 是参照。把它们改小绝不能让闸门变松。"""
        data = CG.load_baseline()
        rc, _ = CG.judge({"product": [1000, 400, 12], "chain": [200, 10, 6]},
                         data["product_floor"], data["chain_floor"])
        self.assertEqual(rc, 1, "产品档 60% 必须红，不管基线里写的 measured 是多少")


if __name__ == "__main__":
    unittest.main(verbosity=2)
