# -*- coding: utf-8 -*-
"""诊断覆盖闸门（tools/diag_coverage_check.py）的判据。

为什么这道闸门值得单独一份判据
------------------------------
它守的是负责人 2026-10-13 那句要求：**每次发版都必须检查，别让他隔几版手动对齐一次
诊断包**。`outputs/诊断导出逐版核查-v1.4.27至v1.4.66-2026-10-12.md` 的账是 39 个实发
版本里 29 个引入了当时远程读不出来的状态，而全量回归两年全绿 —— 接线这件事从来没被
任何关卡看过。所以本文件的第一职责不是"闸门会不会红"，而是**闸门不许在尺子坏掉的
时候装作什么都没发生**：闭包算成空集、只扫到几个模块、取不到本轮区间，三种都必须
拒绝下结论（退 2），绝不许退化成"没改动 = 通过"（台账 G24 那一族）。

判据为什么走主进程直调而不是子进程
--------------------------------
AGENTS §6 第 10 条与 [[project-chain-coverage-import-trap]] 两边都要顾：这工具自己
就是链上 `tools/` 的一档账，用子进程跑等于它自己的 400 行不进覆盖率，反过来把整档
拖低。直调既把账记回来，也让每条断言真的落在被测函数上。只有"未知参数退 2""闸门
在真仓库上的退出码"这类**退出码**判据值得起子进程（它们本来就要证明 CLI 形状）。

尺子的两处实测形状（不是猜的）
----------------------------
`state_bearing_modules` 曾经只看名字是否命中词表，实测把 `sf_glass._TOP_WINDOW_LEVEL`
这类**设计常量**算成"运行态已在包里"，闸门于是往宽松那一侧说谎。现在它要求"会被
改写"（函数里 `global`/重新赋值/下标写入）或"容器字面量"。`sf_bootstrap` 就是那
条反向对照：它有一条命中词表的顶层名 `_TEMP_ROOTS`，但永远不会被改写 —— 收进来
等于给它发了一张免检牌。
"""

import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))

import diag_coverage_check as gate        # noqa: E402  链上工具不在包内，只能按路径取
import sf_constants                       # noqa: E402
from tests._base import SFCase            # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
DECL = ROOT / gate.DECL_FILE


class GateJudgesTheRealRepo(SFCase):
    """尺子在真仓库上的形状：三档各该有哪些模块。"""

    def test_curated_closure_sees_the_aliases_and_the_mixins(self):
        """闭包必须**别名感知**并跟着 FloatWindow 的 mixin 走。

        这条是实测踩出来的：按 `sf_notify.` 属性访问找，会把已经写了 notify.json 的
        sf_notify 判成"读不到"（本轮接线写的是 `from sf_notify import delivery_stats
        as _notify_delivery_stats`，再把函数本身当引用传进去）。少一条判据，尺子就会
        一整轮一整轮地说假话。
        """
        curated = gate.curated_modules(ROOT)
        for name in ("sf_notify", "sf_universe", "sf_calendar", "sf_update",
                     "sf_glass", "sf_presence", "stock_float_window"):
            self.assertIn(name, curated,
                          "%s 的现场确实在包里，闭包却读不到 —— 闸门会向人要一条无意义申报" % name)

    def test_bound_method_callback_is_part_of_the_diagnostic_closure(self):
        with tempfile.TemporaryDirectory(prefix="sf_diag_callback_") as directory:
            root = pathlib.Path(directory)
            (root / "stock_float_window.py").write_text(
                "import sf_presence\n"
                "class FloatWindow:\n"
                "    def guard(self):\n"
                "        return sf_presence.snapshot()\n"
                "    def _export_diagnostics(self):\n"
                "        return _field('guard', self.guard)\n", encoding="utf-8")
            (root / "sf_presence.py").write_text("def snapshot():\n    return {}\n", encoding="utf-8")
            self.assertIn("sf_presence", gate.curated_modules(root))

    def test_stateful_requires_really_being_written(self):
        """正向：真计数器要认；反向：只有一条常量的模块不许认。"""
        stateful = gate.state_bearing_modules(ROOT)
        self.assertIn("sf_notify", stateful, "_DELIVER_COUNTS 是模块级可变计数器，必须认")
        self.assertIn("sf_universe", stateful, "_REJECTS 同上")
        self.assertNotIn("sf_bootstrap", stateful,
                         "它只有一条永不被改写的 _TEMP_ROOTS：认成「有状态」就是给它发免检牌")

    def test_instance_held_only_counts_objects_built_in_init(self):
        """正向：`__init__` 里建起来、一直活着的对象（`sf_anim_driver.AnimDriver`）算在包里。
        反向：编辑器窗口点开才建、关掉就是 None，导出那一下常常不在包里 → 不许算。
        运行时维护实例现在有 active/failure_count 等可采集状态，不能再固定排除模块。"""
        trees = {name: gate.module_tree(ROOT, name) for name in gate.product_modules(ROOT)}
        trees = {k: v for k, v in trees.items() if v is not None}
        held = gate.instance_held_modules(trees, gate.classes_with_state(trees))
        self.assertIn("sf_anim_driver", held)
        self.assertNotIn("sf_editor", held,
                         "第一版按全文扫赋值，把 sf_editor 判成「状态已在包里」，而 self._editor 关着时是 None")
        self.assertIn("sf_maintenance", held, "持续持有的维护调度状态必须纳入自动诊断")

    def test_imported_closure_reaches_almost_every_product_module(self):
        loaded = gate.imported_modules(ROOT)
        self.assertGreaterEqual(len(loaded), gate.MIN_PRODUCT_MODULES,
                                "装机版里加载的产品模块少于下限：import 闭包那一层断了")
        self.assertIn("sf_diag", loaded, "自动采集层自己必须进包（否则 state.json 永远出不来）")

    def test_head_version_follows_the_working_tree_not_only_head(self):
        """版本锚点必须先读磁盘：发版时那一笔常常还没提交。"""
        self.assertEqual(gate.head_version(ROOT), sf_constants.APP_VERSION,  # discipline-ok(R1): 比的就是唯一来源本身，写死数字才是这条规矩要拦的
                         "闸门读到的版本与产品不同源：申报段会永远匹配不上")

    def test_previous_bump_lands_on_a_real_older_version(self):
        # A public root snapshot has no private predecessor. Test actual Git behavior in a bounded history.
        clean_env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        with tempfile.TemporaryDirectory(prefix="sfdiag_history_") as directory, \
                patch.dict(os.environ, clean_env, clear=True):
            repo = pathlib.Path(directory)

            def git(*args):
                return subprocess.run(["git", *args], cwd=repo, check=True,
                                      capture_output=True, text=True).stdout.strip()

            git("init", "-q")
            git("config", "user.name", "fixture")
            git("config", "user.email", "fixture@example.invalid")
            old_version, current_version = "1.0.0", "1.0.1"
            constants = repo / "sf_constants.py"
            constants.write_text("APP_VERSION = '%s'\n" % old_version)
            git("add", "sf_constants.py")
            git("commit", "-qm", "initial public version")
            old_commit = git("rev-parse", "HEAD")
            self.assertEqual(gate.previous_bump(repo), "", "A root snapshot must not invent an older version")
            constants.write_text("APP_VERSION = '%s'\n" % current_version)
            git("add", "sf_constants.py")
            git("commit", "-qm", "next version")
            prev = gate.previous_bump(repo)
            self.assertEqual(prev, old_commit)
            self.assertEqual(gate.version_at(repo, prev), old_version)
            self.assertNotEqual(gate.version_at(repo, prev), gate.head_version(repo))

    def test_the_shipped_declaration_block_is_structurally_valid(self):
        """申报清单本身不许写成解析不出来的样子：指令只有 skip、模块名必须真存在、
        理由非空。写错形状的后果是**闸门看不见这条申报**，于是它在发版时才红。"""
        text = DECL.read_text(encoding="utf-8")
        blocks = [line.split(None, 1)[1].strip()
                  for line in text.splitlines() if line.startswith("version ")]
        self.assertTrue(blocks, "清单里一个版本段都没有")
        products = set(gate.product_modules(ROOT))
        curated = gate.curated_modules(ROOT)
        current_version = sf_constants.APP_VERSION
        declared_names = []
        in_current = False
        for line in text.splitlines():
            if line.startswith("version "):
                in_current = line.split(None, 1)[1].strip() == current_version
                continue
            if not in_current or not line.startswith("skip "):
                continue
            parts = line.split(None, 2)
            self.assertGreaterEqual(len(parts), 3, "%s 少了理由（理由为空即拒）" % line)
            self.assertIn(parts[1], products, "%s 申报了一个不存在的模块：闸门不会认这条" % line)
            self.assertNotIn(parts[1], curated,
                             "%s 明明已经被导出面读到，却来申报「不需要进包」：这是攒免检牌" % line)
            declared_names.append(parts[1])
        self.assertGreaterEqual(len(blocks), 1)
        # 刻意不断言"段里必须有 skip"：一条都不需要的轮次是正常状态（本轮就是两条都不用
        # 报），硬要它非空等于逼人编一条假申报来喂判据。也刻意不在用例里跑 `check(ROOT)`：
        # 本轮改动集由版本锚点算，开发期算出来的是"上一轮 + 这一轮"那一份账，它要由发版时
        # 的闸门自己算，不该被钉成永久断言（这条是复核之后改的：初版正是这么写的，
        # 于是清单刚改成空段这条就红，红得与代码无关）。
        declared, problems = gate.parse_declarations(ROOT, blocks[-1])
        self.assertEqual(problems, [], "最新一段自身解析不过：发版时必然红")
        loaded = gate.imported_modules(ROOT)
        trees = {n: t for n in gate.product_modules(ROOT) if (t := gate.module_tree(ROOT, n)) is not None}
        auto = gate.state_bearing_modules(ROOT) | gate.instance_held_modules(
            trees, gate.classes_with_state(trees))
        for module, reason in declared.items():
            self.assertIn(module, products, "%s 申报了一个不存在的模块：闸门不会认这条" % module)
            self.assertTrue(reason.strip(), "%s 的理由为空（理由为空即拒）" % module)
            self.assertNotIn(module, curated, "%s 的现场其实在包里：那是攒免检牌" % module)
            self.assertFalse(module in loaded and module in auto,
                             "%s 会被自动采集带走，不该申报「不需要进包」" % module)
        for name in declared_names:
            self.assertIn(name, declared, "%s 写在版本段外面，闸门根本读不到" % name)


    def test_check_refuses_instead_of_passing_when_the_ruler_is_broken(self):
        """扫描范围空掉时必须**拒绝下结论**（refuse 非空），而不是给一个"通过"。
        台账 G24 那一族：空输入当通过。"""
        body = gate.check(pathlib.Path("/nonexistent-repo-please-ignore"), version="9.9.9",
                          changed={"sf_notify"})
        self.assertTrue(body["refuse"], "只扫到 0 个模块就下结论，等于闸门变成橡皮图章")
        self.assertFalse(body["ok"], "拒答的同时绝不能报通过")


class GateDeclarationParsing(SFCase):
    """申报段的版本作用域（台账 G26 那条"无主申报被下一版继承"）。"""

    def _tmp_repo(self, text):
        """申报段写在**临时目录**里判，不去改仓库里那份真清单：
        `parse_declarations(root, version)` 本来就按 root 取文件，没有理由污染工作区。"""
        work = pathlib.Path(tempfile.mkdtemp(prefix="sf_diagdecl_"))
        self.addCleanup(lambda: __import__("shutil").rmtree(work, ignore_errors=True))
        (work / gate.DECL_FILE).write_text(text, encoding="utf-8")
        return work

    def test_only_the_current_version_block_is_read(self):
        repo = self._tmp_repo("version 1.0.1\nskip sf_utils 上一轮的理由\n"
                              "version 1.0.2\nskip sf_runtime 本轮的理由\n")
        declared, problems = gate.parse_declarations(repo, "1.0.2")
        self.assertEqual(declared, {"sf_runtime": "本轮的理由"})
        self.assertNotIn("sf_utils", declared, "上一版的申报必须失效")
        self.assertEqual(problems, [])

    def test_empty_reason_and_unknown_directive_are_problems(self):
        repo = self._tmp_repo("version 9.9.9\nskip sf_utils\nnote sf_runtime 这不是合法指令\n")
        _declared, problems = gate.parse_declarations(repo, "9.9.9")
        self.assertEqual(len(problems), 2, "空理由与不认识的指令都必须点名，一条都不能静默")


class GateSelfCheck(SFCase):
    """闸门自己必须能证明它会失败（AGENTS §3.1「链上工具的参数形状规矩」）。"""

    def test_self_test_passes_and_actually_plants_ten_failures(self):
        """直调 `self_test()`，同时钉住"种错条数"：条数掉了说明有人删了一条判据，
        而删掉的那条正是当初实测抓不到那一族缺陷的那条。"""
        failures = gate.self_test(ROOT)
        self.assertEqual(failures, 0, "自检里 %d 处种错没被抓到" % failures)
        text = (ROOT / "tools" / "diag_coverage_check.py").read_text(encoding="utf-8")
        self.assertGreaterEqual(text.count("\n        # S"), 11,
                                "种错条数少于一开始立的十一处：闸门失去了一段可失败性")

    def test_empty_round_passes_with_a_warning_instead_of_a_false_accusation(self):
        """复核 F1：本轮没有产品模块改动时，闸门必须**如实标注并放行**，而不是把清单
        里那些正当申报判成"过期"——那种红会把人推向改清单，而不是看懂结论。"""
        repo = pathlib.Path(tempfile.mkdtemp(prefix="sf_diagempty_"))
        self.addCleanup(lambda: __import__("shutil").rmtree(repo, ignore_errors=True))
        for name, body in gate.base_fixture().items():
            (repo / name).write_text(body, encoding="utf-8")
        (repo / gate.DECL_FILE).write_text(
            "version 9.9.9\nskip sf_utils 纯函数层\nskip sf_bootstrap 结论走 runtime.json\n",
            encoding="utf-8")
        result = gate.check(repo, version="9.9.9", changed=set())
        self.assertTrue(result["empty_round"], "零改动必须如实标注")
        self.assertTrue(result["ok"], "零改动不是失败")
        self.assertEqual(result["stale_declarations"], [],
                         "空轮次里不许反过来指控清单写了过期申报")

    def test_unknown_argument_exits_two_and_does_not_run_the_default(self):
        proc = subprocess.run([sys.executable, str(ROOT / "tools" / "diag_coverage_check.py"),
                               "--dele-everything"], capture_output=True, text=True, cwd=str(ROOT))
        self.assertEqual(proc.returncode, 2, "未知参数必须退 2，绝不落到默认动作")
        self.assertIn("未知参数", proc.stdout + proc.stderr)


class GateOnTheReleaseChain(SFCase):
    """3.9 关的位置：核验必须排在不可逆动作之前（同 TestReleaseChainCallsTheGate 的理由）。"""

    def test_diag_gate_runs_before_dmg_install_and_ota(self):
        text = (ROOT / "build_release.sh").read_text(encoding="utf-8")
        at = text.find("diag_coverage_check.py")
        self.assertGreater(at, 0, "发版链不再核验诊断覆盖")
        for marker, what in (("hdiutil create", "生成 DMG"),
                             ('cp -R "$PROJ/dist/$APP_BUNDLE" "$APP_DST"', "替换 /Applications"),
                             ("publish_update_release.py", "切换线上 OTA 清单")):
            pos = text.find(marker)
            self.assertGreater(pos, 0, "找不到 %s，脚本结构变了，这条断言要重新设计" % what)
            self.assertLess(at, pos, "诊断覆盖核验排在 %s 之后 —— 那已经撤不回了" % what)

    def test_diag_gate_failure_aborts_the_chain(self):
        """红必须中止发版，而不是打一行字继续往下走。"""
        text = (ROOT / "build_release.sh").read_text(encoding="utf-8")
        at = text.find("diag_coverage_check.py")
        tail = text[at:at + 320]
        self.assertIn("exit 1", tail, "核验失败必须中止发版")


if __name__ == "__main__":
    unittest.main(verbosity=2)
