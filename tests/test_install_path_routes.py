# -*- coding: utf-8 -*-
"""首装路线的判据本身，每次提交都跑一遍（轻夹具，不碰 /Applications、不弹任何窗口）。

分工是这样切的：
  · `tests/test_installer_script_branches.py` 验**安装脚本自己**的四条行为（不用造卷）。
  · `tools/verify_install_paths.py` 的九条路线验**真产物装进真 DMG**之后用户手上剩什么，
    跑在发版链 3.8（要 ditto 71MB 真产物、造 HFS+ 卷）。
  · 这里补中间那一层：把**同一批路线函数**用几 KB 的签名替身跑在发版链之外，
    于是"判据自己会不会变成哑门"这件事在每次提交与 CI 上都能被看到，
    而不是等到发版那天才发现某条路线一直返回 True。

为什么轻夹具是正当的而不是缩水：路线要判的是图标、显示名、可执行位、标签是否递归清干净、
codesign 有没有牙、旧版有没有被动过、locale 站在哪一侧 —— 这几件事没有一件依赖产物是 71MB。
真产物那一档继续宣称"用户拿到的那一份"（默认档），这里只宣称"判据抓得住"。

前提坏掉时如实 skip 并说原因（这台机器不能 ad-hoc 签名 / 没有 UTF-8 locale /
LaunchServices 不可用），不假装通过。
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import verify_install_paths as VIP                  # noqa: E402

try:
    from _base import SFCase                         # noqa: E402
except ImportError:                                  # 以包形式被 discover 导入时
    from tests._base import SFCase                   # noqa: E402


class RouteCase(SFCase):
    def setUp(self):
        super().setUp()
        if not VIP.user_lang():
            self.skipTest("本机没有 UTF-8 locale：站在安全一侧跑这些路线没有意义")
        self._tmp = tempfile.TemporaryDirectory(prefix="sf_routes_")
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name)

    def assertRoute(self, pair, note):
        ok, detail = pair
        self.assertTrue(ok, "%s —— %s" % (note, detail))


class TestRoutesInProcess(RouteCase):
    """六条路线的主进程版本（轻夹具）。红 = 判据抓到缺陷，或脚本/夹具真的坏了。"""

    def test_R0_名字同源(self):
        self.assertRoute(VIP.check_name_source(), "R0 应当过：两代顶层名都钉着")

    def test_R1_curl_一行零放行(self):
        self.assertRoute(VIP.check_curl_route(self.work), "R1 应当过：curl 路线天生无标签")

    def test_R2_卷内双击安装(self):
        self.assertRoute(VIP.check_dmg_launcher_route(self.work, fixture="light"),
                         "R2 应当过：脚本能双击、装完递归无标签、日志落盘")

    def test_R2b_老用户升级(self):
        self.assertRoute(VIP.check_upgrade_route(self.work, fixture="light"),
                         "R2b 应当过：旧版整份进废纸篓、新版在位")

    def test_R2d_坏包只报什么都没动(self):
        self.assertRoute(VIP.check_corrupt_source_route(self.work, fixture="light"),
                         "R2d 应当过：校验排在破坏之前")

    def test_R2e_清不掉的标签不许报成功(self):
        self.assertRoute(VIP.check_untaggable_scope_route(self.work, fixture="light"),
                         "R2e 应当过：清不掉的标签会如实失败并把旧版放回原位")

    def test_R2c_旧版在跑时停下等(self):
        ok, detail = VIP.check_running_instance_route(self.work, fixture="light")
        if not ok and ("不能 ad-hoc 签名" in detail or "LaunchServices" in detail
                       or "拉不起替身" in detail or "替身没能起来" in detail):
            self.skipTest("这台机器的 LaunchServices / 签名不可用，这一条测不了：%s" % detail[-200:])
        self.assertTrue(ok, "R2c 应当过：旧版在跑就必须停下且什么都没动 —— %s" % detail)

    def test_R4_助手认两代包名(self):
        self.assertRoute(VIP.check_helper_accepts_both_names(self.work),
                         "R4 应当过：两代顶层名都能解到签名校验")


class TestRoutesHaveTeeth(RouteCase):
    """判据必须能红：这里只种**最便宜的一处**错，真验的是路线不是橡皮图章。

    （完整的一览表在 `verify_install_paths.py --self-test` 里，15 处种错并发跑。）
    """

    def test_核验只看包根时_R2e_必须红(self):
        body = VIP.launcher_script().read_text(encoding="utf-8")
        blind = body.replace('/usr/bin/xattr -rl "$STAGE"', '/usr/bin/xattr -l "$STAGE"')
        if blind == body:
            self.fail("种错没落地：脚本里找不到那条递归核验（判据本身已经变了）")
        ok, detail = VIP.check_untaggable_scope_route(self.work, script_text=blind,
                                                      fixture="light")
        self.assertFalse(ok, "把核验改成只看包根之后 R2e 还是绿的 —— 这道判据是哑门：%s" % detail)

    def test_关卡退回C_locale时_R2必须红(self):
        ok, detail = VIP.check_dmg_launcher_route(self.work, force_lang="C",
                                                  fixture="light")
        self.assertFalse(ok, "关卡站在安全一侧却不报错，等于没测这一族缺陷：%s" % detail)


if __name__ == "__main__":
    unittest.main()
