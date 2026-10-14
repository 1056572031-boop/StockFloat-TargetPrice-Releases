#!/usr/bin/env python3
"""闸门换到"干净机器"上必须如实说没测 —— 不许崩，也不许把没跑算成通过。

起因是 2026-10-10 云端那两条红之一：`tools/verify_install_paths.py --self-test`
里 S2 那条变异要复制"本机装好的真产物"，而云端那台 runner 是全新的空机器
（/Applications 与 dist/ 里什么都没有）。`real_app()` 按约定抛 NoFixture，
旧实现只在"跑五条路线"那段接住它，**自检那段没接** —— 于是异常穿出 main()，
退出码 1，链上用例把它读成"发版链上的闸门自证失败"。
看起来像产物坏了，实际是夹具不在；而"闸门说假话"这一族里，这是最难查的一种：
它红得很有说服力。

修完之后必须锁住三件事，缺一条都可能退回原样：
  1. 缺夹具 → 记成"没跑"，整关仍然绿，但大声 WARNING 并点名是哪几条；
  2. 非前提类异常 → 记红（否则"闸门自己坏了"会被读成"没夹具，跳过"）；
  3. 一条都没跑起来 → 拒绝给结论（没有输入不等于全过）。
"""
from __future__ import annotations

import contextlib
import io
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
for _p in (str(ROOT), str(ROOT / "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import verify_install_paths as VIP  # noqa: E402

try:
    from _base import SFCase  # noqa: E402
except ImportError:  # 以包形式被 discover 导入时
    from tests._base import SFCase  # noqa: E402


def _verdict(cases, prefix):
    """按标签前缀取那条变异的三态结果。"""
    for label, got, note in cases:
        if label.startswith(prefix):
            return got, note
    raise AssertionError("标签 %s 从结果里消失了 —— 判据被删掉了一条" % prefix)


class CleanMachineStillTellsTheTruth(SFCase):
    """把"这台机器没装过程序"搬到测试里，看闸门怎么说自己。"""

    def test_缺真产物时自检档一条都不许落(self):
        """真跑一遍全部变异，只把"取真产物"那一步抽掉。

        2026-10-13 这一条的预期**反过来**了，原因是自检档改用几 KB 的签名替身造卷
        （`light_signed_app`）：判据要验的是图标、显示名、标签递归、codesign 有没有牙、
        locale 站在哪一侧，没有一件依赖产物是 71MB。于是云端那台干净机器从
        "S2 记成没跑、其余照常" 变成 "15 处全跑"。
        旧断言（S2 必须是"没跑"）现在恰好会掩盖这件事，所以这里断言的是：
        一条都不许被记成"没跑"、每一处都真抓到、数量不许掉回个位数（判据被删的证据）。

        为什么仍然必须真跑而不是喂假结果：CI 上红的是真实组合，手工拼的列表测不到
        "异常从哪一层冒出来、有没有被记账"（这条规矩没变）。
        """
        work = pathlib.Path(tempfile.mkdtemp(prefix="sf_gate_clean_"))
        self.addCleanup(shutil.rmtree, str(work), True)
        with patch.object(VIP, "real_app",
                          side_effect=VIP.NoFixture("测试注入：本机没有真产物")):
            cases = VIP.self_test(work / "st")
        absent = [label for label, got, _ in cases if got is None]
        self.assertEqual(absent, [],
                         "自检档里还有变异依赖真产物（轻夹具没接上）：%s" % absent)
        missed = [label for label, got, _ in cases if got is False]
        self.assertEqual(missed, [], "这些种错没被抓到：%s" % missed)
        self.assertGreaterEqual(len(cases), 13,
                                "变异数量掉到 %d 处：有判据被删掉了" % len(cases))
        for prefix in ("S1", "S2", "S6", "S7", "S8", "S9 ", "S9b", "S10"):
            _verdict(cases, prefix)      # 标签消失 = 判据被删，这条比数条数更准

    def test_缺真产物时默认档要如实说哪几条没跑(self):
        """默认档（发版链 3.8 跑的那九条）**仍然**要真产物 —— 它宣称的是用户拿到的那一份。

        两条口径必须分开说：自检档宣称"判据抓得住错"，默认档宣称"这一版装得上"。
        轻夹具只该用在前面那一档。
        """
        work = pathlib.Path(tempfile.mkdtemp(prefix="sf_gate_clean_default_"))
        self.addCleanup(shutil.rmtree, str(work), True)
        buf = io.StringIO()
        with patch.object(sys, "argv", ["verify_install_paths.py"]), \
                patch.object(VIP, "real_app",
                             side_effect=VIP.NoFixture("测试注入：本机没有真产物")), \
                contextlib.redirect_stdout(buf):
            rc = VIP.main()
        out = buf.getvalue()
        self.assertEqual(rc, 0, "没夹具不是缺陷，红得没有依据：%s" % out[-400:])
        self.assertIn("跳过", out, "没跑的那几条没被点名")
        self.assertIn("没测到的不算通过", out, "结论里把没跑当成了通过")
        self.assertNotIn("一条都没跑成", out)

    def test_unexpected_error_is_red_not_skip(self):
        def _boom():
            raise RuntimeError("闸门自己坏了（非前提类）")

        _, got, note = VIP.mutate_case("X 一条变异", _boom)
        self.assertFalse(got,
                         "非前提类异常被记成%s —— "
                         "闸门自己崩了必须红，不许伪装成「没夹具、跳过」" % got)
        self.assertIn("闸门自己坏了", note, "异常原样没带出来，事后查不到坏在哪儿")

    def test_missing_fixture_is_neither_pass_nor_fail(self):
        def _absent():
            raise VIP.NoFixture("没有可复制的真产物")

        _, got, note = VIP.mutate_case("X 一条变异", _absent)
        self.assertIsNone(got)
        self.assertIn("没有可复制的真产物", note)


class HonestVerdict(SFCase):
    """main() 的记账：三态怎么汇总成退出码与打印。"""

    def _run_self_test_branch(self, cases, argv=("--self-test",)):
        buf = io.StringIO()
        with patch.object(sys, "argv", ["verify_install_paths.py", *argv]), \
                patch.object(VIP, "self_test", return_value=list(cases)):
            with contextlib.redirect_stdout(buf):
                rc = VIP.main()
        return rc, buf.getvalue()

    def test_all_caught_is_green_and_says_so(self):
        cases = [("S%d 种错" % i, True, "") for i in range(1, 8)]
        rc, out = self._run_self_test_branch(cases)
        self.assertEqual(rc, 0, out)
        self.assertIn("7 项抓到", out)
        self.assertNotIn("WARNING", out, "一条都没缺时不许喊话，否则喊叫会贬值")

    def test_missing_fixture_stays_green_but_shouts(self):
        cases = [("S1 种错", True, ""), ("S2 种错", None, "前提缺失，这条没跑：没有真产物")]
        rc, out = self._run_self_test_branch(cases)
        self.assertEqual(rc, 0, out)
        self.assertIn("WARNING", out,
                      "有判据没跑却安静通过 —— 这正是「闸门说假话」的形状")
        self.assertIn("S2 种错", out, "WARNING 必须点名是哪一条没跑")
        self.assertIn("没测到的不算通过", out)

    def test_any_missed_is_red(self):
        cases = [("S1 种错", True, ""), ("S2 种错", False, "假门：居然还是绿的")]
        rc, out = self._run_self_test_branch(cases)
        self.assertEqual(rc, 1, "抓不到种错的判据必须红：%s" % out)
        self.assertIn("S2 种错", out)

    def test_zero_input_refuses_to_conclude(self):
        """七条全被"缺夹具"吞掉时不许报通过。

        这一条是给"关卡范围失效"准备的（同 test_discipline_scan 的 min_files 红线）：
        换一台什么都没有的机器，整关可以安静地零输入通过。
        """
        cases = [("S%d 种错" % i, None, "前提缺失，这条没跑") for i in range(1, 8)]
        rc, out = self._run_self_test_branch(cases)
        self.assertEqual(rc, 1, "一条都没跑起来却给了通过：%s" % out)
        self.assertIn("拒绝给结论", out)
        self.assertNotIn("变异自检通过", out)


class UnknownArgumentRefusesToAct(SFCase):
    """打错的参数不许落到默认动作上（默认动作 = 跑五条真核验）。"""

    def test_typo_does_not_run_the_default_routes(self):
        buf = io.StringIO()
        with patch.object(sys, "argv", ["verify_install_paths.py", "--slf-test"]), \
                patch.object(VIP, "check_name_source") as first_route, \
                patch.object(VIP, "self_test") as mutated:
            with contextlib.redirect_stdout(buf):
                rc = VIP.main()
        self.assertEqual(rc, 2, "未知参数必须退 2（既不是通过也不是失败）：%s" % buf.getvalue())
        first_route.assert_not_called()
        mutated.assert_not_called()
        self.assertIn("不认识", buf.getvalue())

    def test_pyobjc_gate_refuses_a_typoed_flag(self):
        """签名核对那枚此前对参数一概不看：--slf-test 会跑成正式核验并返回 0。

        链上用例判"有没有可用自检"看的是 rc==2，所以一枚把拼错当成"正常跑一遍"的
        闸门，会让整条链以为自检做过。

        必须起子进程：这一枚有 312 条语句，在主进程 import 它只为调一个参数校验，
        会把整个模块记进覆盖率链上档的分母而几乎全 miss —— 实测把该档从 66% 拖到
        62.69%（AGENTS §6 第 10 条的记账口径，反方向同样成立）。判据本来就是退出码。
        """
        cp = subprocess.run([sys.executable, str(ROOT / "tools" / "pyobjc_sig_check.py"),
                             "--slf-test"],
                            capture_output=True, text=True, cwd=str(ROOT), timeout=60)
        self.assertEqual(cp.returncode, 2,
                         "未知参数必须退 2，不许滑进正式核验那一档：%s"
                         % (cp.stdout + cp.stderr)[-200:])
        self.assertIn("不认识", cp.stdout + cp.stderr)


class EveryRouteSkippedRefusesToConclude(SFCase):
    """五条路线一条都没跑成时，默认档也必须拒绝说"通过"。

    2026-10-12 全方位复核 F5：自检档早就有这条红线（not ran_cases → 退 1），
    默认档却只看 failed —— 于是一台什么都没有的机器能换来一句"核验通过"。
    """

    def _raise(self):
        """路线函数会被传 work 目录，替身必须收参数。"""
        def _boom(*_args):
            raise VIP.NoFixture("测试注入：这台机器没有夹具")
        return _boom

    def test_zero_pass_is_red_not_green(self):
        names = tuple(VIP.route_names())   # 名单唯一来源是路线表，这里不再各写一份
        buf = io.StringIO()
        with patch.object(sys, "argv", ["verify_install_paths.py"]), \
                contextlib.redirect_stdout(buf):
            with patch.multiple(VIP, **{n: self._raise() for n in names}):
                rc = VIP.main()
        out = buf.getvalue()
        self.assertEqual(rc, 1, "一条都没跑成却给了通过：%s" % out)
        self.assertIn("拒绝给结论", out)
        self.assertNotIn("核验通过", out)

    def test_one_route_that_ran_is_enough_to_conclude(self):
        """反向：只要真跑成一条，就照旧给结论（否则这条红线会变成新的"永远红"）。"""
        names = tuple(VIP.route_names())   # 同上

        def _ok(*_args):
            return True, "跑过了"

        buf = io.StringIO()
        patches = {n: self._raise() for n in names}
        patches["check_name_source"] = _ok
        with patch.object(sys, "argv", ["verify_install_paths.py"]), \
                patch.multiple(VIP, **patches), \
                contextlib.redirect_stdout(buf):
            rc = VIP.main()
        out = buf.getvalue()
        self.assertEqual(rc, 0, out)
        self.assertIn("真跑 1 条", out, "跑了几条要写在结论里，别让人自己数")


class BaselineRatchetAppliesToEveryBaseline(SFCase):
    """三枚基线工具里此前只有两枚真的比较旧值 —— 书写纪律那一枚漏了。

    守的是"只降不升"这句话有没有代码执行：`--update` 若不与旧基线逐键比较，
    任何人跑一次就能把违规条数静默抬上去（static_gate 当年 322→317→323 那一幕）。
    """

    def _write(self, tmp, old, new, allow_rise=False):
        import collections
        import json
        import test_discipline_scan as TDS
        path = pathlib.Path(tmp) / "baseline.json"
        path.write_text(json.dumps({"counts": old, "total": sum(old.values())}),
                        encoding="utf-8")
        with patch.object(TDS, "BASELINE", path):
            TDS.write_baseline(collections.Counter(new), allow_rise=allow_rise)
        return json.loads(path.read_text(encoding="utf-8"))["counts"]

    def test_rise_is_refused_and_baseline_untouched(self):
        import json
        import test_discipline_scan as TDS
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "baseline.json"
            path.write_text(json.dumps({"counts": {"R1:a.py": 1}, "total": 1}),
                            encoding="utf-8")
            before = path.read_text(encoding="utf-8")
            with patch.object(TDS, "BASELINE", path):
                self.assertRaises(RuntimeError, TDS.write_baseline,
                                  {"R1:a.py": 2})
            self.assertEqual(path.read_text(encoding="utf-8"), before,
                             "被拒绝的写入仍然动了基线文件")

    def test_rise_with_explicit_flag_and_a_drop_both_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            import collections
            risen = self._write(tmp, {"R1:a.py": 1}, {"R1:a.py": 2}, allow_rise=True)
            self.assertEqual(risen, {"R1:a.py": 2})
            fallen = self._write(tmp, {"R1:a.py": 2}, {"R1:a.py": 1})
            self.assertEqual(fallen, {"R1:a.py": 1}, "下调不该要任何开关")

    def test_repo_baseline_is_not_ratcheted_up_by_this_round(self):
        """整仓基线此刻必须与实测一致：这条防的是"顺手 --update 把额度抬上去"。"""
        import test_discipline_scan as TDS
        counts, errors = TDS.scan(ROOT / "tests")
        self.assertEqual(errors, [], "有测试文件解析失败，这一关对它无效")
        worse, _better = TDS.diff(counts, TDS.load_baseline())
        self.assertEqual(worse, [], "新增违反 §6 书写纪律的用例：%s" % worse)


class LauncherNameIsDiscoveredNotHardcoded(SFCase):
    """卷内双击脚本的名字只有一个来源：installer/ 里那一枚 *.command。

    2026-10-12 把它从 Install-Skyrocket.command 改成「一键安装.command」，而引用它的地方
    有三处（发版脚本、本工具、对外安装说明）。写死任何一处，改名时漏掉的那一处**不会报错**，
    只会让 DMG 里安静地少掉一键安装 —— 用户撞上的是「拖完被 Gatekeeper 拦」那面墙，
    而这道核验当时照样全绿：它找的是自己写死的那个名字，而卷又是它自己造的。
    """

    def _tree(self, names):
        root = pathlib.Path(tempfile.mkdtemp(prefix="sf_launcher_"))
        self.addCleanup(shutil.rmtree, str(root), True)
        (root / "installer").mkdir()
        for n in names:
            (root / "installer" / n).write_text("#!/bin/sh\n", encoding="utf-8")
        return root

    def test_one_script_returns_it(self):
        root = self._tree(["一键安装.command"])
        self.assertEqual(VIP.launcher_script(root).name, "一键安装.command")

    def test_missing_script_refuses_to_guess(self):
        """0 枚必须抛异常：这条路线的前提没了，跳过算通过就是闸门说谎。"""
        with self.assertRaises(RuntimeError) as got:
            VIP.launcher_script(self._tree([]))
        self.assertIn("恰好有一个", str(got.exception))

    def test_second_copy_is_also_a_failure(self):
        """2 枚同样要红：有人复制了一份，进卷的是哪一枚就不由发版人决定了。"""
        with self.assertRaises(RuntimeError):
            VIP.launcher_script(
                self._tree(["一键安装.command", "Install-Skyrocket.command"]))

    def test_repo_itself_ships_exactly_one(self):
        """仓库现状也必须过这条判据 —— 防的是 installer/ 里长出第二枚而没人发现。"""
        found = sorted(p.name for p in ROOT.glob("installer/*.command"))
        self.assertEqual(len(found), 1, "installer/ 里有 %d 枚：%s" % (len(found), found))


if __name__ == "__main__":
    unittest.main(verbosity=2)
