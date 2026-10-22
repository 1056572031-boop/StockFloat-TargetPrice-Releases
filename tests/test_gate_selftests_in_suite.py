# -*- coding: utf-8 -*-
"""把发版链工具的变异自检搬进本进程跑：既锁"闸门会红"，也让它进覆盖率账。

为什么这么分（v1.4.62，用户要求把覆盖率抬到 86%）：
  链上 16 个核验工具都自带 `--self-test`，由 `tests/test_release_gates.py` **起子进程**跑。
  那件事本身是对的（行为判据，空壳 flag 蒙不过去），但 coverage.py 看不见子进程 ——
  于是 `release_feature_check` 23%、`make_update_assets` 16%、`verify_dmg_pyz` 67%…
  账面上像"大片没测"，实际是**记账口径**。AGENTS §6.10 已经为同类事写过一句：
  「子进程不计入 .coverage，这是记账口径，不是没测」。
  这里补的是第二层：在同一进程里再跑一遍自检函数，让它既成为回归的一部分
  （工具哪天把种进去的错漏放了，全量当场红），也让账目对得上真实工作量。

不做的事：
  · `smoke_frozen_app` / `verify_install_paths` 不在这里跑 —— 一个要起产物进程、
    一个会真装 app 到临时目录并动卷挂载，两把子进程放进全量里性价比太低。
  · 不去改 `.venv` 装 coverage 的 subprocess 钩子（那是换机器就失效的环境魔法，
    与本仓"依赖精确锁定 + 可复现"的口径冲突）。
"""

import importlib.util
import io
import os
import sys
import unittest
from contextlib import redirect_stdout, redirect_stderr

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

try:
    from _base import SFCase                           # noqa: E402
except ImportError:                                     # pragma: no cover
    from tests._base import SFCase                     # noqa: E402

# 在这里跑的 = 纯分析型自检：不 mount 卷、不装 app、不起 GUI 进程。
IN_SUITE = (
    "stable_signing",            # Native certificate-pinned identity and planted signature failures.
    "verify_card_corners",       # independent corner pixels: matched / second-card / blank evidence
    "public_docs_check",        # missing/stale documents and GPL completeness
    "public_release",           # secrets in reachable old history and privacy boundaries
    "verify_macos_compat",      # real Mach-O deployment/architecture positive and negative controls
    "release_feature_check",     # 本轮特征清单：新增/旧符号/缺 version 各种错
    "make_update_assets",        # canonical 签名、sha256、本版说明 150 字上限
    "verify_dmg_pyz",            # 产物字节码核验器本身
    "bundle_closure_check",      # import 闭包对拍
    "dist_version_check",        # 版本闸门（防拿旧 dist 打新 DMG）
    "test_discipline_scan",      # §6 书写纪律七条各种一处必须全抓到
    "static_gate",               # ruff/mypy 棘轮 + 基线不许偷偷上调
    "quality_trend",             # 逃逸台账 14 种失守形态
    "sweep_temp_apps",           # 临时副本清扫的五条边界
    "gate_stamp",                # 关卡时间戳 11 项变异
    "coverage_gate",             # 覆盖率分档门禁（与上面同族：基线只降不升、空输入不放行）
    "desktop_folder_sync",       # 桌面分发目录对账（发版链 7.1）：全部种错在临时目录里造，不碰真实桌面
    "diag_coverage_check",       # 诊断覆盖闸门（2026-10-13 起，发版链 3.9）：纯静态分析，
    #                              十处种错全在临时目录里造微型仓库，不碰真仓库历史
    "render_install_guide",      # 对外安装说明渲染器（v1.4.61 补的那 12 项）
    "apply_installer_icon",      # 卷内安装脚本的图标：贴法与"读回比像素"的判据（v1.4.63 补）
    "verify_face_presence",      # 自动隐藏窗口的三条计数口径 + 三档判据本身能不能失败
    "quality_evidence",           # 修改单、receipt、manifest 与产物身份的离线证据协议
    "release_state",             # 临时 Git 中验证跳阶段、源码变化、目录篡改的真实拒绝
    #                              （2026-10-12 补：边脸放宽与 1.5 米闸门落地时，这套判据
    #                              是唯一能长期回答"那条曲线还成立吗"的东西；它此前只有
    #                              人工调用，等于任何一轮都可以悄悄不跑。亮度那一头在
    #                              没有人脸素材的树上打印「跳过」并说明理由，不记失败。）
)

# 有意排除的，写清楚为什么（不是忘了）
SKIPPED = {
    "coverage_provenance": "真实微型数据库及故障样本已由 test_coverage_provenance 在主进程逐项执行；self_test 再载入测试模块会递归运行测试套件，不在此重复",
    "change_coverage_gate": "自检使用真实临时 Git 与 coverage 收集器；test_change_coverage_gate 已直调核心检查并验证 CLI 正反，避免在全量里重复切换嵌套收集器",
    "smoke_frozen_app": "要真起打包产物进程，全量里不该再开一份 GUI 生命周期",
    "verify_install_paths": "会真解包安装到临时目录并挂 DMG 卷，已由发版链 3.8 单独跑",
    "verify_bottom_gap_px": "本身已是 run_tests.sh 第 0.5 关，整脚本直接跑过",
    "test_runner": "它就是本套测试的运行器，自跑无意义",
    "publish_update_release": "端到端发布另有 tests/test_publish_end_to_end.py 用本地假服务跑",
    "pyobjc_sig_check": "self_test(found) 要主流程喂它自己的扫描结果，且必须在全新解释器里量编码"
                        "（同进程会被产品侧 registerMetaDataForSelector 污染成一面镜子）—— "
                        "这条正是它自己 docstring 里的教训，只能按 run_tests.sh 第 0.3 关那样起子进程跑",
}


def _load(name):
    path = os.path.join(ROOT, "tools", name + ".py")
    spec = importlib.util.spec_from_file_location(name + "_in_suite", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ChainToolSelfTestsRunInProcess(SFCase):
    """每个工具的 self_test() 必须在本进程里跑过且返回成功。

    判据落在返回值上，不读源码文本：自检函数哪天被改成"永远返回 0"，
    由它自己种的错形被抓不到时 —— 那正是 `test_discipline_scan`/`static_gate`
    这些工具自己反复防的那类事，这里用同一把尺子量它们。
    """

    def test_every_listed_tool_exposes_a_self_test(self):
        missing = []
        for name in IN_SUITE:
            mod = _load(name)
            if not callable(getattr(mod, "self_test", None)):
                missing.append(name)
        self.assertEqual(missing, [],
                         "这些链上工具没有可调用的 self_test()，本用例的清单要重新设计：%s" % missing)

    def test_in_list_matches_disk(self):
        """清单不许悄悄漏项：tools/ 下带 self_test 的链上工具，要么在跑、要么写明跳过理由。"""
        import re
        chain = set()
        for script in ("build_release.sh", "run_tests.sh"):
            with io.open(os.path.join(ROOT, script), encoding="utf-8") as f:
                chain |= set(re.findall(r"tools/([a-z_0-9]+\.py)", f.read()))
        chain = {c[:-3] for c in chain}
        unaccounted = sorted(n for n in chain if n not in IN_SUITE and n not in SKIPPED)
        self.assertEqual(unaccounted, [],
                         "发版链上出现了没安排的工具（要么加入本进程清单，要么写明为什么不起）：%s"
                         % unaccounted)

    def test_all_self_tests_pass_in_process(self):
        failed = []
        for name in IN_SUITE:
            try:
                mod = _load(name)
            except Exception as exc:                         # noqa: BLE001
                failed.append("%s（导入失败 %r）" % (name, exc))
                continue
            fn = getattr(mod, "self_test", None)
            if not callable(fn):
                failed.append("%s（没有 self_test）" % name)
                continue
            buf_out, buf_err = io.StringIO(), io.StringIO()
            saved_argv = list(sys.argv)
            sys.argv = [name + ".py", "--self-test"]
            try:
                with redirect_stdout(buf_out), redirect_stderr(buf_err):
                    rc = fn()
            except SystemExit as exc:                        # 允许用 sys.exit 表达失败
                rc = exc.code if isinstance(exc.code, int) else 1
            except Exception as exc:                         # noqa: BLE001
                rc = "抛异常 %r" % (exc,)
            finally:
                sys.argv = saved_argv
            if rc not in (0, None):
                failed.append("%s → %s｜%s" % (name, rc,
                                               (buf_err.getvalue() or buf_out.getvalue())[-160:]))
        self.assertEqual(failed, [],
                         "这些发版链核验工具的自检在本进程里没通过（它们会红，所以本轮结论不算数）：\n  "
                         + "\n  ".join(failed))

    def test_skipped_tools_stay_documented(self):
        """被排除的工具要一直有理由 —— 三年后没人记得为什么不起它。"""
        for name, why in SKIPPED.items():
            self.assertTrue(why.strip(), "%s 的跳过理由是空的" % name)
            self.assertNotIn(name, IN_SUITE, "%s 既写了跳过理由又在跑，说明清单漂了" % name)


if __name__ == "__main__":
    unittest.main()
