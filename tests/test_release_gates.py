# -*- coding: utf-8 -*-
"""三道防线自身的不变量用例（v1.4.33 随防线一起落地）。

刻意只补**四类核心不变量**，不铺开：
  1) 防线自己不说假话 —— 每个新闸门都跑一遍"种错必须被抓到"的变异自检；
  2) 基线不能塌 —— 静态棘轮基线、静默兜异常基线一旦为空/被清空，等于没有关卡；
  3) 静默失效必须留痕 —— log_swallow 要同时进 debug.log 和 last_failures.json；
  4) 启动身份与首帧锚点必须真的写出来 —— 产物冒烟只认这两行日志。

为什么用 subprocess 跑 --self-test：那些自检的**全部内容**就是"作为一个独立
命令时它会不会失败"，在进程内调函数就丢掉了它作为发版链一环的真实行为。
"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import sf_logging                                    # noqa: E402
import sf_constants as C                              # noqa: E402
from dist_version_check import dist_version         # noqa: E402
try:
    from _base import SFCase                         # noqa: E402
except ImportError:  # 以包形式被 discover 导入时
    from tests._base import SFCase                   # noqa: E402

# 本轮产物的路径：名字的唯一来源是 sf_constants.APP_DISPLAY_NAME。
# 2026-10-12 全方位复核 F8：下面两处此前写死 `dist/实时股价-目标价版.app`，而 v1.4.55 起
# 产物叫 Skyrocket.app —— `if app.exists()` 因此恒假，"含真产物那一层"在任何机器上
# 一次都没执行过，用例照样报绿。改名漏改判据是这一族的老毛病（助手包名是第六处）。
DIST_APP = ROOT / "dist" / (C.APP_DISPLAY_NAME + ".app")


def _run_tool(script, *args):
    """跑一个 tools/ 下的核验脚本，返回 (exitcode, stdout+stderr)。"""
    cp = subprocess.run([sys.executable, str(ROOT / "tools" / script)] + list(args),
                        capture_output=True, text=True, cwd=str(ROOT), timeout=180)
    return cp.returncode, cp.stdout + cp.stderr


def stale_source_module(bundle, root=ROOT):
    """A matching version does not make a pre-edit dist bundle current."""
    from verify_card_corners import code_shape
    from verify_dmg_pyz import CORE_MODULES
    required = {"stock_float_window", *CORE_MODULES}
    paths = [*root.glob("sf_*.py"), root / "stock_float_window.py", root / "renderer.py"]
    for path in paths:
        packaged = bundle.code(path.stem)
        if packaged is None and path.stem not in required:
            continue  # Developer-only modules need not be part of the application.
        current = compile(path.read_text(encoding="utf-8"), str(path), "exec")
        if packaged is None or code_shape(packaged) != code_shape(current):
            return path.stem
    return None


class BundleSourceFreshness(SFCase):
    def current_code(self, name):
        path = ROOT / (name + ".py")
        return compile(path.read_text(encoding="utf-8"), "packaged/" + path.name, "exec")

    def test_identical_executable_source_is_current_despite_build_paths(self):
        self.assertIsNone(stale_source_module(types.SimpleNamespace(code=self.current_code)))

    def test_changed_function_body_is_stale_without_needing_a_version_change(self):
        def code(name):
            return compile("def changed(): return None", "old", "exec") if name == "sf_glass" else self.current_code(name)
        self.assertEqual(stale_source_module(types.SimpleNamespace(code=code)), "sf_glass")

    def test_required_source_module_missing_from_the_bundle_is_stale(self):
        def code(name):
            return None if name == "sf_constants" else self.current_code(name)
        self.assertEqual(stale_source_module(types.SimpleNamespace(code=code)), "sf_constants")

    def test_unpackaged_developer_module_does_not_make_a_current_bundle_stale(self):
        def code(name):
            return None if name == "sf_camera_check" else self.current_code(name)
        self.assertIsNone(stale_source_module(types.SimpleNamespace(code=code)))


class TestGatesTellTheTruth(unittest.TestCase):
    """第一/二道防线的**自检**必须长绿：它们一旦退化，整轮测试的结论都不算数。"""

    def test_static_gate_self_test_passes(self):
        rc, out = _run_tool("static_gate.py", "--self-test")
        self.assertEqual(rc, 0, "静态闸门自检失败（说明它对未定义名/属性拼错/"
                                "常量 getattr 已经抓不到了）：\n%s" % out)

    def test_static_gate_current_state_is_clean(self):
        rc, out = _run_tool("static_gate.py")
        self.assertEqual(rc, 0, "静态闸门当前就不该红（红了说明发版链第 0 关在拦人）：\n%s"
                         % out[-2500:])

    def test_bundle_closure_self_test_passes(self):
        """打包闭包自检必须**在没有产物的机器上也跑**（复核 F8 的同一条教训）。

        原来这条把断言写在 `if dist/ 有产物` 里面，CI 上永远不判 —— 等于只在打包机上有效。
        工具已改成两层：不传 app 时验收集器与缺失/放行判定（离线层），
        有产物时再加验真产物层。所以这里默认不带 app 跑，产物在场时再补一层。
        """
        rc, out = _run_tool("bundle_closure_check.py", "--self-test")
        self.assertEqual(rc, 0, "打包闭包自检（离线层）失败：\n%s" % out)
        self.assertIn("自检通过", out)
        app = DIST_APP
        if app.exists():
            # dist/ 常驻的是上一轮产物时，它不能代表当前源码的 import 闭包。
            # 旧包缺少本轮新 hiddenimport（例如 sf_diag）是正常的；正式发版链会在
            # PyInstaller 重建后直接对当前产物执行闭包核验，不能让旧包把提交前的
            # 自检变成假红。
            if dist_version(app) != C.APP_VERSION:
                self.skipTest("dist/ 产物是旧版本 %r，等待本轮构建后再验真产物层"
                              % dist_version(app))
            from verify_dmg_pyz import Bundle
            bundle = Bundle(app)
            try:
                stale = stale_source_module(bundle)
            finally:
                bundle.dispose()
            if stale:
                self.skipTest("dist/ 同版本但 %s 与当前源码不同，等待重建后再验真产物层" % stale)
            rc2, out2 = _run_tool("bundle_closure_check.py", str(app), "--self-test")
            self.assertEqual(rc2, 0, "打包闭包自检（含真产物层）失败：\n%s" % out2)
            self.assertIn("真产物层", out2, "有产物却没跑到真产物层，等于少验一层")

    def test_release_feature_gate_rejects_a_vacuous_list(self):
        """空清单/旧版本号必须被拒 —— 否则第 3.6 步又是一道永远过的关。

        复核 F8：原来这条把断言写在 `if dist/ 有产物` 里面，CI 上永远不判，
        等于只在打包机上有效。判据本身不需要产物（产物核验是下一步），
        所以改成进程内直接喂清单文本断言，任何环境都真的跑。
        """
        import release_feature_check as RF
        rc, out = _run_tool("release_feature_check.py", "--self-test")
        self.assertEqual(rc, 0, "本轮特征闸门自检失败：\n%s" % out)
        want = RF.current_version()
        cases = [
            ("空清单", "# 什么都没填\n"),
            ("只有版本号", "version %s\n" % want),
            ("特征一条都没有（只有注释）", "# 忘了填\n# version %s\n" % want),
        ]
        for title, body in cases:
            with tempfile.TemporaryDirectory() as td:
                f = pathlib.Path(td) / "checks.txt"
                f.write_text(body, encoding="utf-8")
                problems = RF.gate(RF.parse_checks(f), want)
                self.assertTrue(problems, "清单「%s」竟然被放行 → 第 3.6 步形同虚设"
                                % title)

    def test_smoke_judge_is_not_a_rubber_stamp(self):
        rc, out = _run_tool("smoke_frozen_app.py", "--self-test")
        self.assertEqual(rc, 0, "冒烟判定器自检失败（坏报告被判成通过）：\n%s" % out)

    def test_version_gate_can_fail(self):
        """版本闸门此前全仓只有一句**注释**提到它，从没被证明过会红。

        守的是什么：`--skip-build` 时 dist/ 是上一轮残留产物，若它是旧版本却仍被打成
        新 DMG 装机，用户拿到"版本号对、内容是旧的"包且全程零报错（v1.2.95 第 8 轮）。
        这道闸门红不了，等于那条事故路径重新畅通。
        为什么必须用子进程：发版链调用它的全部意义就是**退出码**，进程内调函数拿不到。
        """
        rc, out = _run_tool("dist_version_check.py", "--self-test")
        self.assertEqual(rc, 0, "版本闸门自检失败（说明它已经不会拒绝旧产物了）：\n%s" % out)
        app = DIST_APP
        if app.exists():
            rc2, out2 = _run_tool("dist_version_check.py", str(app), "0.0.0")
            self.assertEqual(rc2, 1, "拿真产物对一个不可能的版本号必须判不匹配：\n%s" % out2)

    def test_bottom_gap_criteria_is_not_self_referential(self):
        """像素关卡的间距上限不许跟着产品常量走。

        旧写法是 `gap <= BOTTOM_ROW_GAP + 3`：常量被改大，判据就跟着变钝 ——
        与 verify_dmg_pyz 无脑 PASS、探针把全屏前置做成永真是同一个根子（判据自指）。
        现在上限是 GAP_CEILING_PX 绝对值，自检用合成文字带喂进 judge()，
        不建窗口、不导入产品模块（那两条本身就是解耦的证明，写在自检里）。
        """
        rc, out = _run_tool("verify_bottom_gap_px.py", "--self-test")
        self.assertEqual(rc, 0, "像素关卡自检失败（有一种「改坏」没被抓到）：\n%s" % out)

    def test_escape_retro_gate_rejects_a_missing_declaration(self):
        """发版链 3.7：没有本轮复盘声明必须被拒，且台账 44 条自身要自洽。

        这条是"会自我进化的防线"的牙齿：逃逸复盘不做就出不了包。
        它同时也是对台账的 schema 校验 —— 有人把条目改坏（归不到防线、
        时刻倒挂、回填估算值）都在这里红。
        """
        rc, out = _run_tool("quality_trend.py", "--self-test")
        self.assertEqual(rc, 0, "复盘闸门没过自己的变异自检，它可能已是条永不失败的检查：\n%s" % out)
        rc2, out2 = _run_tool("quality_trend.py", "--gate", "0.0.0")
        self.assertEqual(rc2, 1, "不存在的版本号必须被拒（没有复盘声明）：\n%s" % out2)
        self.assertIn("rounds 里没有", out2, "拒绝的理由应当是缺复盘声明，不是别的：\n%s" % out2)
        rc3, out3 = _run_tool("quality_trend.py", "--check")
        self.assertEqual(rc3, 0, "台账条目本身必须自洽（归到防线、时刻不倒挂、null 带理由）：\n%s" % out3)

    def test_every_chain_tool_can_prove_it_fails(self):
        """发版链上的每个 `tools/*.py` 必须自带 `--self-test`，且**真跑起来必须过**。

        规矩是 2026-10-09 负责人拍的板：链条上的核验工具必须自带 --self-test。
        守的是什么：任何一枚核验器如果不会红，它给的 PASS 就是空票 —— 本仓为此
        栽过至少四次（台账 G01~G05：verify_dmg_pyz 写死旧特征连续十几个版本无脑 PASS
        约 54 个版本、release_checks 一条永不可能命中的伪特征、探针用 Chrome 菜单判全屏
        导致前 6 轮前置全是假的、位图缓存核验空转却报"36 组比对全部零差异"）。
        以前这条只活在文档里，而"只写进文档的规矩"在这仓的战绩就是失守。

        为什么必须起子进程跑而不是扫源码文本：找 `--self-test` 字符串只能证明
        "写了这几个字"，证明不了它会失败、也证明不了它能给出结论。这里要的是行为 ——
        只加一个空壳 flag 蒙不过去（argparse 退 2 就算没自检）。
        范围从 `build_release.sh` / `run_tests.sh` 现扫（闭包实测无新增），
        将来链条上加脚本，这里自动多守一个，不需要谁记得改清单。
        """
        import re
        chain = set()
        for name in ("build_release.sh", "run_tests.sh"):
            text = (ROOT / name).read_text(encoding="utf-8")
            chain |= set(re.findall(r"tools/([a-z_0-9]+\.py)", text))
        self.assertGreaterEqual(len(chain), 10, "链条脚本没扫到几个：%s" % sorted(chain))
        no_flag, failed = [], []
        for fname in sorted(chain):
            if not (ROOT / "tools" / fname).exists():
                no_flag.append("%s（脚本不存在）" % fname)
                continue
            rc, out = _run_tool(fname, "--self-test")
            if rc == 2:
                no_flag.append("%s（不认 --self-test，退出码 2）" % fname)
            elif rc != 0:
                failed.append("%s\n%s" % (fname, out[-700:]))
        problems = []
        if no_flag:
            problems.append("这些发版链上的核验器没有可用的 --self-test（没人能证明它会红）：\n  "
                            + "\n  ".join(no_flag))
        if failed:
            problems.append("这些核验器的自检自己没过：\n" + "\n".join(failed))
        self.assertFalse(problems, "\n".join(problems))


class TestBaselinesDoNotCollapse(unittest.TestCase):
    """基线一旦被清空或写歪，闸门就会变成"永远 PASS"—— 直接测这件事。"""

    def test_static_baseline_exists_and_is_not_empty(self):
        import static_gate as SG
        data = SG.load_baseline()
        self.assertGreater(data.get("total", 0), 100,
                           "基线总条数异常小：八成是被误清空了，那样 A 级以下的错误"
                           "全部畅通无阻")
        self.assertTrue(all(isinstance(v, int) and v >= 0
                            for v in data["counts"].values()),
                        "基线值必须是条数（整数）")

    def test_baseline_matches_current_reality_within_one_round(self):
        """基线与实测的差只允许是"债务减少"，不允许出现新键。

        这条把"忘了 --update 就提交"的窗口关死：谁改了检查器版本/规则集，
        这里会立刻红，而不是等到别人提交时被误红。
        """
        import static_gate as SG
        _hard, ratchet, _raw = SG.collect()
        data = SG.load_baseline()
        worse, _better = SG.diff_against_baseline(ratchet, data)
        self.assertEqual(worse, [], "基线已落后于代码现状：\n  " + "\n  ".join(worse))


class SwallowTraceCase(SFCase):
    """第三道防线：静默失效必须留下**两处**痕迹（日志行 + 滚动摘要）。"""

    def setUp(self):
        super().setUp()
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self._old_log = sf_logging.LOG_PATH
        sf_logging.LOG_PATH = str(self.tmp / "debug.log")
        sf_logging.reset_log_throttle()
        sf_logging.reset_last_failures()

    def tearDown(self):
        sf_logging.LOG_PATH = self._old_log
        sf_logging.reset_log_throttle()
        sf_logging.reset_last_failures()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)
        super().tearDown()

    def test_swallow_lands_in_log_and_summary(self):
        sf_logging.log_swallow("stock_float_window._raise_tool_window",
                               NameError("name '_objc_deps' is not defined"))
        text = (self.tmp / "debug.log").read_text(encoding="utf-8")
        self.assertIn("SWALLOWED stock_float_window._raise_tool_window", text)
        self.assertIn("_objc_deps", text)
        summary = json.loads((self.tmp / "last_failures.json").read_text(encoding="utf-8"))
        self.assertIn("stock_float_window._raise_tool_window", summary["failures"])
        rec = summary["failures"]["stock_float_window._raise_tool_window"]
        self.assertEqual(rec["n"], 1)
        self.assertIn("NameError", rec["exc"])

    def test_summary_follows_the_redirected_log_path(self):
        """摘要路径必须**运行时**跟着 LOG_PATH 算。

        写死成导入期常量 = 跑测试时往用户 ~/Library/Application Support 里
        写文件，本仓为这类污染吃过亏（一次回归把用户 bg 从 glass 改成 translucent）。
        """
        self.assertEqual(str(sf_logging.last_failures_path()),
                         str(self.tmp / "last_failures.json"))

    def test_repeat_swallow_accumulates_and_exit_flush_is_loyal(self):
        """限流是手段、不是谎言：内存一直累计，退出那一笔必须补齐。

        刻意不直接 sleep 5 秒等限流窗口过去（AGENTS 第 6 节第 3 条）。
        """
        for _ in range(3):
            sf_logging.log_swallow("sf_x.f", ValueError("boom"))
        mid = json.loads((self.tmp / "last_failures.json").read_text(encoding="utf-8"))
        self.assertEqual(mid["failures"]["sf_x.f"]["n"], 1,
                         "同一站点在限流窗口内不该反复重写文件（热路径会写爆磁盘）")
        self.assertTrue(sf_logging.flush_last_failures_now())
        done = json.loads((self.tmp / "last_failures.json").read_text(encoding="utf-8"))
        self.assertEqual(done["failures"]["sf_x.f"]["n"], 3,
                         "退出刷新的职责就是把次数补齐，否则摘要在替故障撒谎")
        sf_logging.reset_last_failures()
        self.assertFalse(sf_logging.flush_last_failures_now(),
                         "没有记录时不该凭空造文件")

    def test_internal_fault_counter_is_not_silent(self):
        """日志子系统自己的失败必须有计数的地方（那 1 处纯 pass 就是这么记账的）。"""
        self.assertIsInstance(sf_logging.internal_faults(), dict)
        sf_logging._note_internal_failure("unit_probe")
        self.assertEqual(sf_logging.internal_faults().get("unit_probe"), 1)


class StartupAnchorCase(SFCase):
    """启动身份行 + 首帧锚点：产物冒烟只认这两行，所以必须真写得出来。"""

    def setUp(self):
        super().setUp()
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self._old_log = sf_logging.LOG_PATH
        sf_logging.LOG_PATH = str(self.tmp / "debug.log")
        sf_logging.reset_log_throttle()

    def tearDown(self):
        sf_logging.LOG_PATH = self._old_log
        sf_logging.reset_log_throttle()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)
        super().tearDown()

    def test_banner_carries_version_platform_and_every_feature(self):
        import sf_constants
        self.assertTrue(sf_logging.log_startup_banner())
        line = (self.tmp / "debug.log").read_text(encoding="utf-8")
        self.assertIn("STARTUP v%s run=%s" % (sf_constants.APP_VERSION,
                                              sf_logging.RUN_ID), line)
        self.assertIn("frozen=0", line)          # 测试跑在源码环境，不是冻结产物
        # 每个功能开关都要出现在横幅里：查"功能为什么没生效"时这是第一手证据
        for name in sf_logging._FEATURES:
            self.assertIn("%s=" % name, line)

    def test_timestamp_carries_date_and_run_id(self):
        sf_logging.log_line("锚点探针")
        line = (self.tmp / "debug.log").read_text(encoding="utf-8").strip()
        # 只有时:分:秒的旧格式无法回答"这是哪天发生的"，轮转后尤其致命
        self.assertRegex(line, r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} "
                              r"[0-9a-f]{6}\] INFO: 锚点探针$")

    def test_first_frame_anchor_is_written_by_a_real_paint(self):
        """不变量：**窗口真的画完一帧**，日志里就必须出现 first-frame painted。

        这是产物冒烟的判据之一，所以它必须由"真渲染"来守，而不是由源码字符串来守。
        """
        import stock_float_window as m
        import sf_quote
        app = self.ensure_qapp()               # 复用全局 QApplication，不登记它
        self.track_patch(patch.object(m, "load_config",
                                      return_value=("sh600519", 100, 100, 200,
                                                    "translucent")))
        self.track_patch(patch.object(m, "save_config", lambda *a, **k: None))
        self.track_patch(patch.object(sf_quote, "http_get", lambda *a, **k: ""))
        self.track_patch(patch.object(m, "start_icon_worker", lambda: None))
        self.track_patch(patch.object(m.FloatWindow, "_force_top", lambda self: None))
        win = self.track_widget(m.FloatWindow())
        win.show()
        app.processEvents()
        # 为什么要碰私有成员：锚点是**一次性**的，show() 期间可能已经消耗掉，
        # 必须重新装填才能观察"画完一帧 → 一定留一行"这个因果。没有公开入口。
        win._sf_first_frame_logged = False
        before = self._log_text()
        win.grab()                             # 真实渲染栈：走 paintEvent 本体
        app.processEvents()
        text = self._log_text()[len(before):]
        self.assertIn("first-frame painted %dx%d" % (win.width(), win.height()), text,
                      "画完一帧却没留下锚点行 = 产物冒烟会把好包判成坏包")

    def _log_text(self):
        f = self.tmp / "debug.log"
        return f.read_text(encoding="utf-8") if f.exists() else ""


class TestReleaseChecksFileIsUsable(unittest.TestCase):
    """release_checks.txt 的当前内容必须与本轮版本号同源（发版人改代码时同步）。"""

    def test_current_file_matches_app_version(self):
        import release_feature_check as RF
        parsed = RF.parse_checks(ROOT / "release_checks.txt")
        want = RF.current_version()
        problems = RF.gate(parsed, want)
        self.assertEqual(problems, [],
                         "release_checks.txt 现在就不能用（第 3.6 步会红）：\n  "
                         + "\n  ".join(problems))
        # 至少一条特征落在**产品模块**上，而不是随便一个通用符号
        self.assertTrue(any(m.split("::")[0] in
                            {p.stem for p in ROOT.glob("*.py")}
                            for m in parsed["check"] + parsed["forbid"]),
                        "特征清单里没有一条指向产品模块 —— 这种清单证明不了本轮改动")


if __name__ == "__main__":
    unittest.main()
