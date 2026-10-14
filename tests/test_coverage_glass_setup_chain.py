# -*- coding: utf-8 -*-
"""`_setup_glass` 的真链路（子进程）—— sf_glass 覆盖率专项第三批（遗留 C2 收尾）。

为什么必须真跑一遍：`_setup_glass` 是本仓唯一**在屏幕上建 NSWindow** 的入口，而它的
每一条判据都落在"用户屏幕上有没有这块窗"：

  · 玻璃层建不出来 → 卡片没有深色底，浅色壁纸上白字几乎不可读；
  · 顺序反了（玻璃压住内容）→ 用户报的"只剩背景"那类空壳；
  · 失败收口漏一步 → 桌面上永久留一块**点击穿透**的毛玻璃（v1.4.18 走查补的就是这条）；
  · 置顶复核不收敛 → 每 250ms 全量写一次系统（v1.4.10 的省电结论当场作废），
    而"顺序倒了能自己回来"这条也只能靠它守。

这些在离屏测试进程里全都桩不出来：离屏下 `winId()` 不是 NSView*，把它当指针喂给
`objc_object` 是段错误而不是异常（v1.4.32 产物实测 rc=139）。所以沿用
`tests/_alert_child.py` 的同一条路子 —— 真链路放子进程，父用例只看它打印的标记与退出码。

没有窗口服务的机器（SSH / 某些 CI runner）由 `probe` 预检整体跳过，跳过理由会打印出来，
不假装通过。

本轮实测的一条判据教训（写在 `tests/_glass_child.py` 里，这里也提一句）：合成顺序
**不能**用 `kCGWindowListOptionAll` 的下标判 —— 同级窗口在那份列表里按窗口号排，
本机实测给出"玻璃在上"的错误结论，而产品自己用的 `kCGWindowListOptionOnScreenOnly`
那份才是用户实际看到的那一份。
"""

import pathlib
import subprocess
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from tests._base import SFCase    # noqa: E402

import sf_glass                   # noqa: E402
from sf_constants import BG_LIQUID_GLASS_DIM, LIQUID_GLASS_OPACITY, R  # noqa: E402

_TOP_LEVEL = sf_glass._TOP_WINDOW_LEVEL   # discipline-ok(R5): 层级期望值只有一个来源，取实现里那一份

ROOT = pathlib.Path(__file__).resolve().parent.parent
CHILD = ROOT / "tests" / "_glass_child.py"
# 子进程里只做"建一块玻璃 + 读回它"，正常 2~4 秒。上限压到 50s 是为了让它比
# tools/test_runner.py 的单用例超时（SF_TEST_TIMEOUT，默认 60s）先到期 ——
# 真红了要能看到 stdout 里的 error 标记，而不是被运行器整轮 dump 栈。
TIMEOUT_SECONDS = 50


def _spawn(mode):
    proc = subprocess.run([sys.executable, str(CHILD), mode],
                          capture_output=True, text=True, timeout=TIMEOUT_SECONDS,
                          cwd=str(ROOT))
    markers = {}
    for line in (proc.stdout or "").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            markers[key.strip()] = value.strip()
    return proc, markers


class GlassSetupChainTest(SFCase):
    """真 AppKit 背景层：正常路径建得对，失败路径收得干净。

    继承 SFCase（AGENTS §6 第 7 条）：本文件自己不建 Qt widget（建窗口是子进程的事），
    但基类的登记式清理是「以后往这里加真窗口用例」时的安全网，成本为零。
    """

    @classmethod
    def setUpClass(cls):
        try:
            proc, markers = _spawn("probe")
        except subprocess.TimeoutExpired:
            raise unittest.SkipTest("玻璃预检超时：本机可能没有窗口服务")
        except OSError as exc:
            raise unittest.SkipTest("无法起子进程：%r" % (exc,))
        if markers.get("probe") != "ok":
            raise unittest.SkipTest("玻璃预检未通过（无窗口服务？）probe=%s rc=%s err=%s"
                                    % (markers.get("probe"), proc.returncode,
                                       (proc.stderr or "")[-200:]))

    def _run(self, mode):
        proc, markers = _spawn(mode)
        self.assertNotIn("error", markers,
                         "子进程抛异常：%s（stderr 尾部：%s）"
                         % (markers.get("error"), (proc.stderr or "")[-200:]))
        self.assertEqual(proc.returncode, 0,
                         "子进程退出码非零：%s" % (proc.stderr or "")[-200:])
        return proc, markers

    def test_setup_builds_a_glass_layer_that_stays_under_the_content(self):
        _proc, m = self._run("setup")
        self.assertEqual(m.get("setup"), "True", "玻璃层必须建得出来，建不出来卡片就没有深色底")
        self.assertEqual(m.get("setup_return_matches_state"), "True",
                         "返回值必须与 _glass_window 有没有一致：不一致说明两条判据各说各话")
        self.assertEqual(m.get("glass_created"), "1", "只该建一块背景窗")
        self.assertEqual(m.get("visible"), "True", "背景层必须真的上屏，否则用户看不到深色底")
        self.assertEqual(m.get("same_level"), "True",
                         "玻璃层要与内容窗同级：不同级会在切 Space / 进全屏时被夹住或整体消失")
        self.assertEqual(m.get("ignores_mouse"), "True", "背景层不许拦截鼠标")
        self.assertEqual(m.get("shadow_off_main"), "True", "主窗原生阴影必须关掉，否则窗口外圈出现厚黑边")
        self.assertEqual(m.get("shadow_off_glass"), "True", "玻璃层自己也不许有阴影")
        self.assertEqual(m.get("mask_attached"), "True", "圆角 mask 没挂上就是四角露出方角")
        self.assertEqual(m.get("material"), "21",
                         "材质固定 underWindowBackground：sheet/popover 那几档自带四周描边")
        self.assertEqual(m.get("glass_same_size"), "True",
                         "背景层尺寸要跟内容窗一致，否则深色底会露出一圈边")
        self.assertEqual(m.get("watchdog_sees_this_pair"), "True",
                         "看门狗查不到这一对时，顺序倒了也永远不会被发现、永远不会被纠正")
        self.assertEqual(m.get("glass_on_top"), "False",
                         "_setup_glass 是「先玻璃、后主窗」成对上屏的入口，顺序不能反过来")
        self.assertEqual(m.get("hidden_after_close"), "True",
                         "收摊用 orderOut；close 会让 AppKit 与 PyObjC 抢所有权，"
                         "自动释放池 drain 时崩（v1.2.90 的现场崩溃）")

    def test_a_mask_failure_after_the_layer_is_on_screen_leaves_nothing_behind(self):
        """失败收口：玻璃已经 orderFront，之后才炸 —— 这块窗必须被收回屏幕之外。

        为什么挑"第二次 mask 才炸"：`_setup_glass` 里 orderFront 夹在两次
        `_set_glass_mask` 之间。第一次就失败的话那块窗根本还没上屏，cleanup 分支
        （orderOut + 把主窗阴影补回来）一行都不会走，也就测不到真实故障。
        """
        _proc, m = self._run("setup_fail")
        self.assertEqual(m.get("setup"), "False", "注入失败后必须如实返回 False，由调用方降级")
        self.assertEqual(m.get("mask_attempts"), "2", "没走到第二次说明这条路径压根没测到")
        self.assertEqual(m.get("glass_created"), "1", "失败前那块玻璃必须真的建出来了")
        self.assertEqual(m.get("state_reset"), "True",
                         "_glass_window / _glass_view / _glass_mask_layer 都要归零，"
                         "留着引用会让后续 _close_glass 再也收不到它")
        self.assertEqual(m.get("shadow_back"), "True",
                         "主窗阴影是无条件关掉的，失败时必须补回来，不能一直裸着")
        self.assertEqual(m.get("orphan_not_visible"), "True",
                         "桌面上不许永久留一块点击穿透的毛玻璃 —— 这正是用户看不见、"
                         "系统毫无痕迹的那一类")


    def test_force_top_converges_and_self_heals_an_inverted_pair(self):
        """真窗口下的置顶复核：稳态什么都不写，玻璃倒压上来时判得出、纠得回、再回稳态。

        这条覆盖的是 `_force_top` 里离屏永远走不到的十几行（含 v1.4.35 收尾的
        `orderWindow:relativeTo:`）：离屏拿不到内容窗，整条第一行就 return。
        注入手法与 `tools/glass_occlusion_probe.py` 一致 —— 把玻璃层 orderFront
        到内容窗之上，先证明"真的倒了"，再看下一拍有没有被纠正。

        判"修好了"的前提是**探针这次真的看见了这一对**（两条 probe_sees_pair 断言），
        否则空结果会让这条检查退化成永远不会失败的门。
        """
        _proc, m = self._run("force_top")
        self.assertEqual(m.get("level_after_first_pass"), str(_TOP_LEVEL),
                         "第一拍必须把层级抬到本仓唯一来源那一位")
        self.assertEqual(m.get("glass_level_follows"), "True",
                         "背景层要跟内容窗同级：不同级会在切 Space / 进全屏时被夹住")
        self.assertEqual(m.get("steady_state_converged"), "True",
                         "v1.4.10 的省电结论：体检通过后一次系统写都不发。"
                         "这里不收敛就是每 250ms 全量置顶（盘中多出来的那几次 WindowServer 往返）")
        self.assertEqual(m.get("steady_state_logs_nothing"), "True",
                         "稳态不该写任何 re-top 取证；写了说明判据在把正常状态当故障反复纠正")
        self.assertEqual(m.get("fixes_at_steady"), m.get("fixes_first_pass"),
                         "稳态期间完整置顶的次数不该再增长")
        self.assertEqual(m.get("probe_sees_pair_before_fix"), "True")
        self.assertEqual(m.get("inverted_before_fix"), "True",
                         "注入没生效的话，后面那条 healed 就是在为空结果鼓掌")
        self.assertEqual(m.get("probe_sees_pair_after_fix"), "True")
        self.assertEqual(m.get("healed"), "True",
                         "只剩背景必须是**秒级的瞬态**：顺序倒了要自己回来，不能永久")
        self.assertLessEqual(int(m.get("passes_to_heal", "0")), 5,
                             "自愈的拍数就是看门狗重试的轮次（250ms 一拍）。判据不写成「1 拍」："
                             "`orderWindow:relativeTo:` 要等 WindowServer 把新顺序反映进 "
                             "OnScreenOnly 列表 —— 本机随机序全量轮实测过，只拍一次就读结论"
                             "会偶发读到旧顺序，那是一条会自己咬人的假门")
        self.assertIn("glass above main", m.get("retop_reason", ""),
                      "纠正了却没记是哪一类故障，下次在别的机器上就无法归因")
        self.assertEqual(m.get("fixes_after_fault"), "True", "判出故障的那一拍必须真的动手")
        self.assertEqual(m.get("settles_back_to_steady"), "True",
                         "纠正完要回到什么都不写的稳态，否则会一直写下去")

    def test_liquid_surface_configuration_resize_and_teardown(self):
        _proc, markers = self._run("liquid_setup")
        self.assertEqual(markers["setup"], "True")
        if markers["api_available"] == "True":
            self.assertEqual(markers["backend"], "liquid")
            self.assertEqual(float(markers["opacity"]), LIQUID_GLASS_OPACITY)
            self.assertEqual(int(markers["dim"]), BG_LIQUID_GLASS_DIM)
            self.assertEqual(float(markers["radius"]), float(R))
            self.assertEqual(markers["style"], "0")
            self.assertIn("Dark", markers["appearance"])
        else:
            self.assertEqual(markers["backend"], "legacy")
            self.assertEqual(markers["reason"], "liquid_api_unavailable")
        self.assertEqual(markers["resize_aligned"], "True")
        self.assertEqual(markers["hidden_after_close"], "True")

    def test_mapped_liquid_failure_retries_legacy_without_an_orphan(self):
        _proc, markers = self._run("liquid_late_failure")
        if markers["api_available"] == "False":
            self.skipTest("Native Liquid Glass API is unavailable on this machine")
        self.assertEqual(markers["setup"], "True")
        self.assertEqual(markers["backend"], "legacy")
        self.assertEqual(markers["reason"], "liquid_window_setup_failed")
        self.assertEqual(markers["created"], "2")
        self.assertEqual(markers["failed_window_hidden"], "True")
        self.assertEqual(markers["fallback_visible"], "True")

    def test_liquid_z_order_self_heals_and_returns_to_idle(self):
        _proc, markers = self._run("liquid_force_top")
        self.assertEqual(markers["probe_sees_pair_before_fix"], "True")
        self.assertEqual(markers["inverted_before_fix"], "True")
        self.assertEqual(markers["healed"], "True")
        self.assertEqual(markers["settles_back_to_steady"], "True")


if __name__ == "__main__":
    unittest.main()
