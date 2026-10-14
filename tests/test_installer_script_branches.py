# -*- coding: utf-8 -*-
"""「一键安装」四条分支的快判据（不造 DMG，每次提交与 CI 都跑）。

分工：`tools/verify_install_paths.py` 那几条（R2/R2b/R2c/R2d/R2e）跑在**发版链 3.8**，
它们要真挂卷，因为判据包括"卷内条目长什么样""标签会不会传染"。而那些一条要 9 秒造卷，
放进每次提交的关卡不划算（同 AGENTS §2 对覆盖率档的取舍）。
这里用普通目录当"挂载好的卷"，把**脚本自己的四条行为**钉在主战场：

  1. 干净安装：装上、递归无标签、日志落盘、回显版本。
  2. 老用户升级：旧版整份进废纸篓（不删）、新版在位。
     这一段以前从没被执行过 —— R2 永远装进空目录，于是「有旧版」那条主用途里
     藏着的一枚必现崩溃（`$DEST，`）与一处顺序缺陷（校验排在破坏之后）都没露头。
  3. 旧版**正在运行**：必须停下来等它退出，不许报"装好了"。
     实测手场：旧版在跑时脚本一路 rc=0，而机器上活着的仍是旧一代 ——
     新一代被自家多开守卫 `sys.exit(0)`（`sf_logging._become_single_instance`）静默退出，
     用户看到的就是"按指引装了新版，问题怎么还在"。
  4. 坏包 / 清不掉标签 / 移不动旧版：每条失败都必须"什么都没动"或"把旧版放回原位"，
     且那句话真的打印得出来（**不许**以 unbound variable 崩在指引这一行上）。

所有子进程都强制跑在 `LANG=zh_CN.UTF-8` 下 —— 用户终端那一侧。这台开发机的 shell
不设 LANG，站在安全一侧，绿不算数（机制本身由 `tests/test_shell_cjk_scan.py::TestMechanism` 钉着）。
"""
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import verify_install_paths as VIP                      # noqa: E402

try:
    from _base import SFCase
except ImportError:                  # 以包形式被 discover 导入时
    from tests._base import SFCase

SCRIPT = VIP.launcher_script()
USER_LANG = VIP.user_lang()          # 取不到 UTF-8 locale 的机器如实 skip
# 断言里比的是**夹具的代际**（原位躺着的是旧的那一份，还是新拷的那一份），
# 不是产品版本号 —— 所以写成具名常量，别把 x.y.z 字面量放进 assert
# （AGENTS §6 第 1 条 + 书写纪律 R1；§6 第 1 条守的是"版本唯一来源 sf_constants"）。
OLD_GEN = "0.0.1"
NEW_GEN = "9.9.9"
MIN_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


class InstallerBranchCase(SFCase):
    def setUp(self):
        super().setUp()
        if not USER_LANG:
            self.skipTest("本机没有 UTF-8 locale：站在安全一侧跑这些判据没有意义")
        self._tmp = tempfile.TemporaryDirectory(prefix="sf_installer_branch_")
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name)
        self.home = self.work / "home"
        (self.home / ".Trash").mkdir(parents=True)
        self.target = self.work / "apps"
        self.target.mkdir()
        self.vol = self.work / "vol"
        self.vol.mkdir()
        self._markers = []
        # 收尾顺序：先卸 uchg（否则删不掉），再杀掉可能残留的替身进程。
        self.addCleanup(self._kill_standin)
        self.addCleanup(lambda: VIP.clear_immutable(self.work))

    def _kill_standin(self):
        for marker in self._markers:
            VIP.kill_by_path("sleep " + marker)

    # ---------- 夹具 ----------

    def make_volume_app(self, version=NEW_GEN, corrupt=False,
                        inner_tag=False, immutable_inner=False):
        """卷里那一枚包：签名过、身份对；corrupt / 内层标签按各条判据的需要种。"""
        marker = self._marker()
        app = VIP.make_launchable_app(self.vol, VIP.CURRENT_APP_NAME, version,
                                      marker, bundle_id=VIP.BUNDLE_ID)
        if app is None:
            self.skipTest("这台机器不能 ad-hoc 签名（codesign 不可用）："
                          "替身过不了脚本里那道 codesign 校验，这组判据跑不了")
        if inner_tag:
            inner = app / "Contents" / "Info.plist"
            VIP.stamp_quarantine(inner)
            if immutable_inner:
                VIP.make_immutable(inner)
        if corrupt:
            plist = app / "Contents" / "Info.plist"
            body = plist.read_text(encoding="utf-8")
            plist.write_text(body.replace("APPL", "ZZZZ", 1), encoding="utf-8")
        return app

    def place_old(self, version=OLD_GEN, immutable=False):
        """安装目录里已有的旧版。immutable = 连目录都移不动（标准用户 / 别的账户装的那一份）。"""
        app = VIP.make_fake_app(self.target, VIP.CURRENT_APP_NAME, version=version,
                                bundle_id=VIP.BUNDLE_ID)
        if immutable:
            VIP.make_immutable(app)
        return app

    def _marker(self) -> str:
        """必须是**纯数字**：它会被写进替身载荷变成 `/bin/sleep <marker>`，
        字母参数会让 sleep 当场报错，于是"旧版在跑"这一条变成'替身没能起来'而空跑。"""
        marker = "%05d" % random.randrange(10 ** 4, 10 ** 5)
        self._markers.append(marker)
        return marker

    def run_script(self, **overrides) -> subprocess.CompletedProcess:
        # 跑的是**卷里那一枚副本**，不是仓库里那份：脚本按 `$0` 的所在目录找要装的 app，
        # 直接跑仓库副本会让所有路线都停在"这个卷里没找到 Skyrocket.app"，
        # 测出来的是夹具位置不对，不是脚本行为不对。
        in_vol = self.vol / SCRIPT.name
        shutil.copyfile(SCRIPT, in_vol)
        in_vol.chmod(0o755)
        env = {"PATH": MIN_PATH, "HOME": str(self.home),
               "LANG": USER_LANG, "LC_ALL": USER_LANG, "LC_CTYPE": USER_LANG,
               "SF_INSTALL_DIR": str(self.target), "SF_INSTALL_NO_LAUNCH": "1"}
        env.update({k: str(v) for k, v in overrides.items()})
        return subprocess.run(["/bin/sh", str(in_vol)], capture_output=True,
                              text=True, errors="replace", env=env, timeout=180,
                              cwd=str(self.work))

    def trash_names(self):
        return sorted(p.name for p in (self.home / ".Trash").glob("*"))

    def out(self, proc) -> str:
        return (proc.stdout + proc.stderr)

    # ---------- 1 干净安装 ----------

    def test_干净安装_装上且递归无标签且有日志(self):
        self.make_volume_app(version=NEW_GEN, inner_tag=True)
        proc = self.run_script(SF_INSTALL_ALLOW_RUNNING="1")
        self.assertEqual(proc.returncode, 0, self.out(proc))
        self.assertEqual(VIP.installed_version(self.target / VIP.CURRENT_APP_NAME), NEW_GEN,
                         "装上的不是卷里那一版")
        still, where = VIP.has_quarantine_anywhere(self.target / VIP.CURRENT_APP_NAME)
        self.assertFalse(still, "包内还带着来历标签（清或核验有一边不递归）：%s" % where)
        logs = list((self.home / "Library" / "Logs").glob("*.log"))
        self.assertTrue(logs, "装完了没有安装日志：用户卡住时只能截图，发不出文件")
        body = logs[0].read_text(encoding="utf-8")
        self.assertIn("安装完成", body)
        self.assertIn(NEW_GEN, self.out(proc), "没把装进去的版本号回显给用户")

    # ---------- 2 老用户升级 ----------

    def test_老用户升级_旧版整份进废纸篓且新版在位(self):
        self.make_volume_app(version=NEW_GEN)
        self.place_old(OLD_GEN)
        proc = self.run_script(SF_INSTALL_ALLOW_RUNNING="1")
        self.assertEqual(proc.returncode, 0, self.out(proc))
        self.assertEqual(VIP.installed_version(self.target / VIP.CURRENT_APP_NAME), NEW_GEN,
                         "原位不是新版")
        trash = self.trash_names()
        self.assertEqual(len(trash), 1, "废纸篓里应当恰好一份旧版：%s" % trash)
        self.assertTrue(trash[0].endswith(".old"), "旧版的名字要一眼认得出是上一次装的：%s" % trash)
        self.assertEqual(VIP.installed_version(self.home / ".Trash" / trash[0]), OLD_GEN,
                         "废纸篓里那份不是原来的旧版（移错了对象）")
        self.assertIn("移到废纸篓", self.out(proc))

    # ---------- 3 旧版正在运行 ----------

    def test_旧版正在运行时必须停下且什么都没动(self):
        self.make_volume_app(version=NEW_GEN)
        marker = self._marker()
        old = VIP.make_launchable_app(self.target, VIP.CURRENT_APP_NAME, OLD_GEN,
                                      marker, bundle_id=VIP.BUNDLE_ID)
        if old is None:
            self.skipTest("codesign 不可用，起不了替身进程")
        rc_open, msg_open = VIP.launch(old)
        if rc_open != 0:
            self.skipTest("LaunchServices 不可用（open 失败）：%s" % msg_open[-160:])
        if not self._wait_for_marker(marker, timeout=10.0):
            self.skipTest("替身进程没能起来，这条判据的前提没了")
        proc = self.run_script(SF_INSTALL_WAIT_SEC="2")
        self.assertNotEqual(proc.returncode, 0,
                            "旧版在跑却报了成功（用户会以为新版已装好）：\n" + self.out(proc))
        body = self.out(proc)
        self.assertIn("正在运行", body, "停下来是对的，但那句话没告诉用户停在哪儿")
        self.assertNotIn("unbound variable", body)
        self.assertEqual(VIP.installed_version(self.target / VIP.CURRENT_APP_NAME), OLD_GEN,
                         "停下来之前就已经动了旧版")
        self.assertEqual(self.trash_names(), [], "还没开始装就把旧版移进了废纸篓")

    def test_等待时间为零时仍明确拒绝运行中的旧版(self):
        self.make_volume_app(version=NEW_GEN)
        marker = self._marker()
        old = VIP.make_launchable_app(self.target, VIP.CURRENT_APP_NAME, OLD_GEN,
                                      marker, bundle_id=VIP.BUNDLE_ID)
        if old is None:
            self.skipTest("codesign 不可用，起不了替身进程")
        rc_open, msg_open = VIP.launch(old)
        if rc_open != 0:
            self.skipTest("LaunchServices 不可用（open 失败）：%s" % msg_open[-160:])
        if not self._wait_for_marker(marker, timeout=10.0):
            self.skipTest("替身进程没能起来，这条判据的前提没了")
        proc = self.run_script(SF_INSTALL_WAIT_SEC="0")
        body = self.out(proc)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("一直没能退出", body)
        self.assertNotIn("unbound variable", body)
        self.assertEqual(VIP.installed_version(self.target / VIP.CURRENT_APP_NAME), OLD_GEN)
        self.assertEqual(self.trash_names(), [])

    def test_等它退出之后自动继续装完(self):
        """等待不是死等：用户退出旧版，脚本要自己接着装完，不需要再双击一次。"""
        self.make_volume_app(version=NEW_GEN)
        marker = self._marker()
        old = VIP.make_launchable_app(self.target, VIP.CURRENT_APP_NAME, OLD_GEN,
                                      marker, bundle_id=VIP.BUNDLE_ID)
        if old is None:
            self.skipTest("codesign 不可用，起不了替身进程")
        rc_open, _ = VIP.launch(old)
        if rc_open != 0:
            self.skipTest("LaunchServices 不可用（open 失败）")
        if not self._wait_for_marker(marker, timeout=10.0):
            self.skipTest("替身进程没能起来")
        import threading
        result = {}

        def _run():
            result["proc"] = self.run_script(SF_INSTALL_WAIT_SEC="20")

        thread = threading.Thread(target=_run)
        thread.start()
        time.sleep(1.5)
        VIP.kill_by_path("sleep " + marker)     # 模拟用户在这一刻退出旧版
        thread.join(60)
        self.assertFalse(thread.is_alive(), "等待循环没能收尾")
        proc = result["proc"]
        self.assertEqual(proc.returncode, 0, self.out(proc))
        self.assertIn("旧版已退出，继续安装", self.out(proc))
        self.assertEqual(VIP.installed_version(self.target / VIP.CURRENT_APP_NAME), NEW_GEN)

    def _wait_for_marker(self, marker: str, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            out = subprocess.run(["/bin/ps", "-Ao", "args"], capture_output=True,
                                 text=True, errors="replace").stdout
            if ("sleep " + marker) in out:
                return True
            time.sleep(0.25)
        return False

    # ---------- 4 四条失败路径都不许留空档 ----------

    def test_坏包只报什么都没动_校验必须排在破坏之前(self):
        self.make_volume_app(version=NEW_GEN, corrupt=True)
        self.place_old(OLD_GEN)
        proc = self.run_script(SF_INSTALL_ALLOW_RUNNING="1")
        self.assertNotEqual(proc.returncode, 0, "坏包居然装成功了")
        body = self.out(proc)
        self.assertIn("什么都没动", body)
        self.assertNotIn("unbound variable", body)
        self.assertEqual(VIP.installed_version(self.target / VIP.CURRENT_APP_NAME), OLD_GEN,
                         "坏包现场里原位那份被换掉了（校验排在了破坏之后）")
        self.assertEqual(self.trash_names(), [],
                         "坏包这条应当什么都没动，废纸篓里却有东西")

    def test_移不动旧版时要打印指引而不是崩在指引那一行(self):
        """这枚就是 2026-10-13 那处必现崩溃的回归锁。

        现场：安装目录可写、但旧版本身被上了 uchg → `mv` 失败 → 走
        「旧版本来在 ${DEST}，但没能移到废纸篓（…）」那一句。
        原写法是 `$DEST，`，bash 3.2 在 UTF-8 locale 下把 `，` 的首字节吞进变量名，
        `set -u` 当场以「DEST\\xEF: unbound variable」终止 —— 用户看到的不是指引，
        而是一行 shell 报错（这个脚本自己的 FR-12 硬约束就是"失败必须打印下一步"）。
        """
        self.make_volume_app(version=NEW_GEN)
        self.place_old(OLD_GEN, immutable=True)
        proc = self.run_script(SF_INSTALL_ALLOW_RUNNING="1")
        body = self.out(proc)
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("unbound variable", body,
                         "指引那一行自己崩了（$DEST， 没写成 ${DEST}，）：%s" % body[-240:])
        self.assertIn("没能移到废纸篓", body, "没把失败原因说清楚：%s" % body[-240:])
        self.assertIn("下一步", body, "没有下一步 = 死路")
        self.assertEqual(VIP.installed_version(self.target / VIP.CURRENT_APP_NAME), OLD_GEN)

    def test_安装目录不可写时要说人话(self):
        self.make_volume_app(version=NEW_GEN)
        self.place_old(OLD_GEN)
        os.chmod(self.target, 0o500)
        self.addCleanup(lambda: os.chmod(self.target, 0o700))
        proc = self.run_script(SF_INSTALL_ALLOW_RUNNING="1")
        body = self.out(proc)
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("unbound variable", body)
        self.assertIn("管理员权限", body, "标准用户撞这道墙时，得知道他需要的是什么")

    def test_等待时间非法时拒绝且不触碰旧版(self):
        self.make_volume_app(version=NEW_GEN)
        self.place_old(OLD_GEN)
        proc = self.run_script(SF_INSTALL_WAIT_SEC="-1", SF_INSTALL_ALLOW_RUNNING="1")
        body = self.out(proc)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("有效的非负整数", body)
        self.assertNotIn("unbound variable", body)
        self.assertEqual(VIP.installed_version(self.target / VIP.CURRENT_APP_NAME), OLD_GEN)
        self.assertEqual(self.trash_names(), [])

    def test_并发双击时第二场安装被原子拒绝(self):
        self.make_volume_app(version=NEW_GEN)
        self.place_old(OLD_GEN)
        checksum = subprocess.run(
            ["/usr/bin/cksum"], input=str(self.target).encode(),
            capture_output=True, check=True).stdout.decode().split()[0]
        lock = Path("/tmp") / (
            "skyrocket-install-%s-%s.lock" % (os.getuid(), checksum))
        lock.mkdir()
        self.addCleanup(shutil.rmtree, lock, ignore_errors=True)
        proc = self.run_script(SF_INSTALL_ALLOW_RUNNING="1")
        body = self.out(proc)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("另一场安装正在进行", body)
        self.assertEqual(VIP.installed_version(self.target / VIP.CURRENT_APP_NAME), OLD_GEN)
        self.assertEqual(self.trash_names(), [])

    def test_清不掉的标签要如实报失败并把旧版放回原位(self):
        """清是递归的（xattr -dr），核验也必须是递归的（xattr -rl）。

        用 uchg 让包内那个文件真的清不动（实测 `xattr -dr` 对它 EPERM）：
        只看包根的核验版本会照样说一句「来历标签已清」并把成功报给用户。
        """
        self.make_volume_app(version=NEW_GEN, inner_tag=True, immutable_inner=True)
        self.place_old(OLD_GEN)
        proc = self.run_script(SF_INSTALL_ALLOW_RUNNING="1")
        body = self.out(proc)
        self.assertNotEqual(proc.returncode, 0,
                            "清不掉的标签被报成了成功：\n" + body[-300:])
        self.assertIn("标签", body)
        self.assertNotIn("unbound variable", body)
        self.assertEqual(VIP.installed_version(self.target / VIP.CURRENT_APP_NAME), OLD_GEN,
                         "失败之后旧版没被放回原位（用户手上应当还有一个能用的程序）")
        trash = self.trash_names()
        self.assertTrue(any(n.endswith(".broken") for n in trash),
                        "坏的那一份应当也被挪开，而不是占着安装位置：%s" % trash)

    # ---------- 5 身份：卷里那枚不是我们的包时不装 ----------

    def test_包标识不是我们的两代之一就不许装(self):
        app = VIP.make_launchable_app(self.vol, VIP.CURRENT_APP_NAME, NEW_GEN,
                                      self._marker(), bundle_id="com.not.ours.app")
        if app is None:
            self.skipTest("codesign 不可用")
        self.place_old(OLD_GEN)
        proc = self.run_script(SF_INSTALL_ALLOW_RUNNING="1")
        body = self.out(proc)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("不是本程序", body)
        self.assertEqual(VIP.installed_version(self.target / VIP.CURRENT_APP_NAME), OLD_GEN,
                         "拒装这条也必须在破坏之前")
        self.assertEqual(self.trash_names(), [])


if __name__ == "__main__":
    unittest.main()
