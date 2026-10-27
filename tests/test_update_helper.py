# -*- coding: utf-8 -*-
"""OTA 更新助手（updater/update_helper.sh）的端到端用例（v1.4.59 起，S6）。

为什么这一层非补不可
--------------------
AGENTS §3 记着一条实测事实：v1.4.55 改名漏改了助手里的包内顶层名，**1.4.55 起已装用户
一次都没能自更新成功，而全量回归一直是绿的** —— 原因是从来没有任何用例跑过这个 shell
助手。改一个包内脚本却全绿，这就是"防线盲区"本身。本轮补 S6（去掉 /usr/bin/python3
依赖、回退包只留一份）时同一条盲区又抓到一处：助手装完之后用来确认新进程存活的
`new_exec` 写死了旧的可执行文件名，改名后**必然**判 restart_new_app_failed 并回滚掉
装好了的新版；它一直没露头，只是因为更早的 archive_layout_invalid 先把流程挡住了。

判据口径
--------
不验"装成功"（那会真的在用户屏幕上拉起 app），验**它停在哪一站**：助手每一步都把
状态写进 result.json，停在预期那一站就是那条路线的证据。夹具 app 一律**故意不签名**，
于是所有正当输入都停在 new_app_signature_invalid —— 这恰好证明解包、命名、软链接边界、
哈希、磁盘预检全过了；而缺陷输入必须停在更早的那一站。
"""
import json
import hashlib
import os
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from _base import SFCase                  # noqa: E402
except ImportError:                           # 以包形式被 discover 导入时
    from tests._base import SFCase            # noqa: E402

HELPER = ROOT / "updater" / "update_helper.sh"
BUNDLE_ID = "com.skyrocket.app"
APP_NAME = "Skyrocket.app"

# 助手在每一站的"停站理由"（result.json 的 message）。测试断言的是站名，不是行号。
STATION_OK_UNTIL_SIGNATURE = "new_app_signature_invalid"


def _make_app(parent: pathlib.Path, name: str = APP_NAME, version: str = "9.9.9") -> pathlib.Path:
    """最小可辨识的 .app 替身，**故意不签名**（见模块 docstring 的判据口径）。"""
    app = parent / name
    (app / "Contents" / "MacOS").mkdir(parents=True, exist_ok=True)
    stem = name[:-4]
    (app / "Contents" / "Info.plist").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0"><dict>'
        '<key>CFBundleIdentifier</key><string>%s</string>'
        '<key>CFBundleShortVersionString</key><string>%s</string>'
        '<key>CFBundleName</key><string>%s</string>'
        '<key>CFBundleExecutable</key><string>%s</string>'
        '<key>CFBundlePackageType</key><string>APPL</string>'
        '</dict></plist>\n' % (BUNDLE_ID, version, stem, stem), encoding="utf-8")
    (app / "Contents" / "MacOS" / stem).write_bytes(b"\0\0\0\0")
    return app


def _zip(app: pathlib.Path, out: pathlib.Path) -> pathlib.Path:
    """ditto -c -k --keepParent：真实 OTA 包就是这么打的，软链接也按链接条目存。"""
    p = subprocess.run(["/usr/bin/ditto", "-c", "-k", "--keepParent",
                        str(app), str(out)], capture_output=True, text=True)
    if p.returncode != 0:
        raise AssertionError("造 zip 失败：" + (p.stderr or p.stdout)[-200:])
    return out


def _sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class _HelperCase(SFCase):
    """公共夹具：一个临时"应用程序"目录 + 一份旧版 + 一次助手调用。"""

    def setUp(self):
        super().setUp()
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="sf_helper_"))
        self.applications = self.tmp / "Applications"
        self.applications.mkdir()
        self.old = _make_app(self.applications, version="1.0.0")
        self.work = self.tmp / "work"
        self.work.mkdir()

    def build_archive(self, mutate=None):
        """造一份 OTA zip；mutate(app) 在被打包前改内容（加软链接/改plist）。"""
        stage = self.work / "stage"
        stage.mkdir()
        app = _make_app(stage)
        if mutate:
            mutate(app)
        return _zip(app, self.work / "ota.zip")

    def run_helper(self, archive, target=None):
        """跑**真助手**，返回它停在的那一站（result.json 的 message）。

        --pid 用一个必然不存在的号，助手立刻认为主程序已退出，不等 20 秒。
        """
        target = target or self.old
        result = self.work / "result.json"
        sha = _sha256(archive)
        proc = subprocess.run(["/bin/sh", str(HELPER),
                               "--pid", "2147483646", "--archive", str(archive),
                               "--sha256", sha, "--version", "9.9.9",
                               "--target", str(target), "--result", str(result)],
                              capture_output=True, text=True, errors="replace",
                              timeout=120)
        if not result.is_file():
            raise AssertionError("助手没写 result.json（rc=%s）：%s" % (
                proc.returncode, (proc.stderr or proc.stdout)[-300:]))
        payload = json.loads(result.read_text(encoding="utf-8"))
        return str(payload.get("message", "")), payload

    def station(self, archive, target=None):
        return self.run_helper(archive, target)[0]


class TestHelperAcceptsGoodArchive(_HelperCase):
    """正当输入：解包/命名/软链接/预检这一整段必须一路走到签名那一站。"""

    def test_plain_app_reaches_signature_check(self):
        got = self.station(self.build_archive())
        self.assertEqual(got, STATION_OK_UNTIL_SIGNATURE,
                         "正常包没走到签名步，说明解包之前的某一步被误拒：" + got)

    def test_relative_symlink_inside_app_is_allowed(self):
        """PyInstaller 用相对软链接承载 dylib —— 一刀切禁链接会把真包拒了。"""
        def add_link(app):
            lib = app / "Contents" / "Frameworks"
            lib.mkdir(parents=True, exist_ok=True)
            (lib / "libreal.dylib").write_bytes(b"\0\0\0\0")
            os.symlink("libreal.dylib", str(lib / "libalias.dylib"))
        got = self.station(self.build_archive(add_link))
        self.assertEqual(got, STATION_OK_UNTIL_SIGNATURE,
                         "包内合法相对软链接被误拒（S6 换实现时最容易改过头的一处）：" + got)

    def test_spaces_and_chinese_names_survive(self):
        """条目名里有空格/中文时，预检不能把名字切错而误判成穿越路径。"""
        def add_odd_names(app):
            d = app / "Contents" / "Resources" / "两个 名字"
            d.mkdir(parents=True, exist_ok=True)
            (d / "a b.txt").write_text("x", encoding="utf-8")
            (d / "说明 文档.txt").write_text("x", encoding="utf-8")
        got = self.station(self.build_archive(add_odd_names))
        self.assertEqual(got, STATION_OK_UNTIL_SIGNATURE,
                         "含空格与中文的条目名被预检误拒：" + got)


class TestHelperRejectsBadArchive(_HelperCase):
    """缺陷输入：必须停在对应那一站，且不许走到替换动作（旧包原位还在）。"""

    def _old_untouched(self):
        return (self.old / "Contents" / "MacOS" / "Skyrocket").read_bytes() == b"\0\0\0\0"

    def test_symlink_escaping_the_bundle_is_rejected(self):
        def escape(app):
            d = app / "Contents" / "MacOS"
            os.symlink("../../../../etc/hosts", str(d / "sneaky"))
        got = self.station(self.build_archive(escape))
        self.assertEqual(got, "archive_symlink_invalid",
                         "逃出 app 的软链接没被拒（zip slip 类缺陷）：" + got)

    def test_absolute_symlink_target_is_rejected(self):
        def absolute(app):
            d = app / "Contents" / "MacOS"
            os.symlink("/etc/hosts", str(d / "abs"))
        got = self.station(self.build_archive(absolute))
        self.assertEqual(got, "archive_symlink_invalid",
                         "指向绝对路径的软链接没被拒：" + got)

    def test_dangling_relative_link_inside_bundle_still_passes(self):
        """包内悬空相对链接放行：目标解析后仍在 app 里，拒它只会误伤真包。

        旧 Python 版（os.path.realpath）就是放行；换实现时要的是**边界**这条不变量，
        不是顺手收紧。这条是那条不变量的反向对照，少了它就分不清"检查写对了"和
        "检查写死了全拒"。
        """
        def dangling(app):
            d = app / "Contents" / "MacOS"
            os.symlink("根本没有这个目标", str(d / "broken"))
        got = self.station(self.build_archive(dangling))
        self.assertEqual(got, STATION_OK_UNTIL_SIGNATURE,
                         "包内悬空相对链接被误拒：" + got)

    def test_wrong_top_level_name_is_rejected(self):
        got = self.station(self._wrong_name_archive())
        self.assertEqual(got, "archive_layout_invalid", "第三种顶层名竟然被接受：" + got)

    def _wrong_name_archive(self):
        stage = self.work / "wrong"
        stage.mkdir()
        app = _make_app(stage, name="Whatever.app")
        return _zip(app, stage / "wrong.zip")

    def test_traversal_entry_name_is_rejected_before_extract(self):
        """条目名里带 ..：预检必须在**解压之前**拒掉，否则 ditto 会往工作区外写东西。"""
        archive = self._zip_with_names([("../evil.txt", b"x"),
                                        (APP_NAME + "/Contents/Info.plist",
                                         b"<plist></plist>")])
        got = self.station(archive)
        self.assertEqual(got, "archive_preflight_failed",
                         "穿越路径条目没被预检拦下：" + got)
        self.assertFalse((self.tmp / "evil.txt").exists(),
                         "既然拦下了，夹具目录外就不该出现被写出来的文件")

    def test_absolute_entry_name_is_rejected(self):
        archive = self._zip_with_names([("/etc/evil.txt", b"x"),
                                         (APP_NAME + "/Contents/Info.plist",
                                          b"<plist></plist>")])
        self.assertEqual(self.station(archive), "archive_preflight_failed")

    def test_hash_mismatch_is_rejected(self):
        """改一个字节 → 哈希对不上，绝不该动用户已装的 app。"""
        archive = self.build_archive()
        before = self.old / "Contents" / "Info.plist"
        keep = before.read_bytes()
        tail = archive.read_bytes()[-1:]
        archive.write_bytes(archive.read_bytes()[:-1] + (b"\n" if tail != b"\n" else b" "))
        sha = _sha256(archive)
        result = self.work / "result.json"
        subprocess.run(["/bin/sh", str(HELPER), "--pid", "2147483646",
                        "--archive", str(archive), "--sha256", "0" * 64,
                        "--version", "9.9.9", "--target", str(self.old),
                        "--result", str(result)], capture_output=True, timeout=120)
        payload = json.loads(result.read_text(encoding="utf-8"))
        self.assertEqual(payload.get("message"), "archive_hash_mismatch")
        self.assertEqual(before.read_bytes(), keep, "哈希不对却把用户的 app 换了")

    def _zip_with_names(self, entries):
        """用 zipfile 造**任意名字**的条目（ditto 造不出穿越/绝对路径，那是被测前提）。"""
        import zipfile
        archive = self.work / ("crafted%d.zip" % len(entries))
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, data in entries:
                zf.writestr(name, data)
        return archive


class TestHelperNeedsNoDevTools(_HelperCase):
    """S6 的初衷：装机版那条自动更新通路不许依赖 Command Line Tools。

    口径：这两条断言查的是**依赖本身**（写不存在的绝对解释器路径 = 在没装开发者工具的
    机器上必失败），不是措辞。为什么不用行为断言代替：无法在不装 CLT 的机器上复现，
    而助手用的是绝对路径，PATH 屏蔽不掉；能观测到的只有"引用了哪些绝对命令"。
    行为面由上面那批用例覆盖同一条代码路径 —— 两边合起来才是完整证据。
    """

    def _helper_text(self):
        return HELPER.read_text(encoding="utf-8")

    def _code_lines(self):
        """只留会被 shell 执行的行：注释里必然提到 /usr/bin/python3（那是本轮改掉的说明），
        按整份文本扫会把围栏变成永远红的关。"""
        return [ln for ln in self._helper_text().splitlines()
                if ln.strip() and not ln.lstrip().startswith("#")]

    def test_helper_does_not_call_python(self):
        offenders = [ln.strip()[:80] for ln in self._code_lines()
                     if any(tok in ln for tok in ("/usr/bin/python", "/usr/local/bin/python",
                                                  "/opt/homebrew/bin/python"))]
        self.assertEqual(offenders, [],
                         "助手又依赖开发者工具里的解释器（那是 Command Line Tools 的桩，"
                         "没装 CLT 的机器上一执行就失败，OTA 会伪装成包本身有问题）：\n  "
                         + "\n  ".join(offenders))

    def test_every_absolute_command_the_helper_calls_exists(self):
        """助手点名的每个绝对路径命令都得是系统件（拼错/被删掉都当场红）。"""
        import re
        text = self._helper_text()
        cmds = set(re.findall(r"(?<![\w./-])(/(?:usr|bin|sbin)/[\w./-]+)", text))
        self.assertGreaterEqual(len(cmds), 8, "一条命令都没扫到，正则或脚本结构变了")
        missing = sorted(c for c in cmds if not os.path.exists(c))
        self.assertEqual(missing, [], "助手引用了本机不存在的命令：" + "、".join(missing))


class TestRollbackPrune(_HelperCase):
    """回退包只留最近一份（S6 第一项）：判据是目录树，不是脚本措辞。

    v1.4.61 起这条**不再是唯一的覆盖**：下面的 TestHelperPositiveRoute 会把助手整条跑到
    installed。那件新测法补的是"真顺序"（先写 result 再清盘），这个抽段法补的是
    "多份、顺序、边界"（三份里留哪一份、只有一份时不许动、目的地不存在要能建出来）——
    两件事各管一段，都留着。抽段法的红线：**只喂助手对外该有的输入**（TARGET/prune_dir），
    替函数把 parent/base 算好就等于把它自己那一步跳过了（上一版正是这样遮住了缺陷）。
    """

    def _load_prune_function(self):
        text = HELPER.read_text(encoding="utf-8")
        begin, end = "# BEGIN prune_rollbacks", "# END prune_rollbacks"
        self.assertIn(begin, text, "助手的 prune_rollbacks 起始标记没了 —— 这条路线又没人测了")
        self.assertIn(end, text, "助手的 prune_rollbacks 结束标记没了")
        body = text.split(begin, 1)[1].split(end, 1)[0]
        self.assertIn("prune_rollbacks()", body, "标记之间不再是那个函数，测试前提变了")
        return body

    def _run(self, names, prune_dir):
        # v1.4.61：这里只喂 TARGET（助手 externally 就该有的输入），不再替函数把
        # parent/base 算好 —— 上一版这么写恰好把「parent 被别人覆盖」那枚真缺陷遮住了。
        target = self.applications / APP_NAME
        script = ("TARGET='%s'\nprune_dir='%s'\n%s\nprune_rollbacks\n"
                  % (target, prune_dir, self._load_prune_function()))
        proc = subprocess.run(["/bin/sh", "-c", script], capture_output=True, text=True,
                              timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr[-300:])

    def _make_rollbacks(self, epochs):
        names = []
        for e in epochs:
            d = self.applications / (".Skyrocket.app.rollback-%d" % e)
            d.mkdir()
            (d / "marker").write_text(str(e), encoding="utf-8")
            names.append(d.name)
        return names

    def _left(self):
        return sorted(p.name for p in self.applications.glob(".Skyrocket.app.rollback-*"))

    def test_keeps_only_the_newest(self):
        prune = self.tmp / "prune"
        prune.mkdir()
        self._make_rollbacks([100, 200, 300])
        self._run([], prune)
        self.assertEqual(self._left(), [".Skyrocket.app.rollback-300"],
                         "该只留最新那份（epoch 最大）")
        moved = sorted(p.name for p in prune.iterdir())
        self.assertEqual(moved, [".Skyrocket.app.rollback-100",
                                 ".Skyrocket.app.rollback-200"],
                         "更早的两份应当被**挪走**（不是删除）到临时目录")

    def test_single_rollback_survives(self):
        """只有一份时它就是当前版本的回退路径，一份都不许动。"""
        prune = self.tmp / "prune"
        prune.mkdir()
        self._make_rollbacks([500])
        self._run([], prune)
        self.assertEqual(self._left(), [".Skyrocket.app.rollback-500"])
        self.assertEqual(list(prune.iterdir()), [], "没有多余的回退包，就不该挪走任何东西")

    def test_no_rollbacks_and_no_target_dir_is_harmless(self):
        prune = self.tmp / "prune"
        prune.mkdir()
        self._run([], prune)
        self.assertEqual(list(prune.iterdir()), [],
                         "没有回退包时不该凭空造出内容（目的地目录可以建，但里面必须空）")

    def test_prune_dir_created_when_missing(self):
        """目的地不存在时函数要能把它建出来 —— 装机版第一次跑到这里就是这种情况。"""
        prune = self.tmp / "prune_not_there"
        self._make_rollbacks([100, 200])
        self._run([], prune)
        self.assertTrue(prune.is_dir(), "临时目的地没建 → mv 全部失败，回退包照样堆在 /Applications")
        self.assertEqual(self._left(), [".Skyrocket.app.rollback-200"])


class _PositiveRouteSupport:
    """正向安装路线夹具与断言辅助，不让普通路线测试被握手类继承。

    上面那个类把函数体抽出来单独跑，它能证明"剪枝算法本身对"，**证明不了"跑到剪枝
    那一刻，parent 还指着安装目录"**。v1.4.61 就是被这一层放过的真缺陷：
    `write_result()` 里那句 `parent="$(dirname "$RESULT")"` 把外层的全局 parent 覆盖成
    result.json 所在目录，于是写完 installed 之后的剪枝扫的是临时目录，三份回退包一条
    都没挪走、一声不吭（实测 trace：`ls -1d '/tmp/.../.Skyrocket.app.rollback-*'`）。
    所以这里必须把助手**跑到 installed**，判据回到目录树与进程本身。

    载荷是最小可启动替身：ad-hoc 签名 + LSUIElement + `/bin/sh` 里跑 sleep（**不用 exec**，
    否则 argv 变成 /bin/sleep，助手的存活复核 pgrep 就认不出它 —— 这件事我第一次手工
    复现时就踩过）。绝不在用户屏幕开窗口，绝不碰 /Applications。
    签名或 LaunchServices 不可用时整体跳过并打印理由（跳过要说为什么，不假装通过）。
    """

    def _stub_app(self, parent, version, ack=None):
        app = parent / APP_NAME
        (app / "Contents" / "MacOS").mkdir(parents=True, exist_ok=True)
        (app / "Contents" / "Info.plist").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0"><dict>'
            '<key>CFBundleIdentifier</key><string>%s</string>'
            '<key>CFBundleShortVersionString</key><string>%s</string>'
            '<key>CFBundleName</key><string>Skyrocket</string>'
            '<key>CFBundleExecutable</key><string>Skyrocket</string>'
            '<key>CFBundlePackageType</key><string>APPL</string>'
            '<key>LSUIElement</key><true/>'
            '</dict></plist>\n' % (BUNDLE_ID, version), encoding="utf-8")
        # 不 exec：让 argv 一直是 <包路径>/Contents/MacOS/Skyrocket，pgrep -f 才认得
        script = "#!/bin/sh\n"
        if ack is not None:
            # 替身扮演「新版程序自己确认了首帧与启动健康采样」：按产品同一形状写 ack。
            ack_path, ack_body = ack
            script += "printf '%%s' '%s' > '%s'\n" % (ack_body, ack_path)
        script += "/bin/sleep 90\n"
        (app / "Contents" / "MacOS" / "Skyrocket").write_text(script, encoding="utf-8")
        os.chmod(app / "Contents" / "MacOS" / "Skyrocket", 0o755)
        sign = subprocess.run(["/usr/bin/codesign", "-s", "-", "--force", "--deep", str(app)],
                              capture_output=True, text=True)
        return app, sign


class TestHelperPositiveRoute(_PositiveRouteSupport, _HelperCase):
    """整条正向路线：真助手 → 装完 → 真把新版拉起来 → 真把陈旧回退包挪走。"""

    def test_installed_route_launches_and_prunes_old_rollbacks(self):
        stub_dir = self.tmp / "probe"
        stub_dir.mkdir()
        probe, sign = self._stub_app(stub_dir, "0.0.1")
        if sign.returncode != 0:
            self.skipTest("这台机器不能 ad-hoc 签名（codesign 不可用）：正向路线跑不了 —— %s"
                          % (sign.stderr or "")[-120:])
        opener = subprocess.run(["/usr/bin/open", str(probe)], capture_output=True, text=True)
        import time
        alive = None
        for _ in range(20):                       # 最多等 4 秒，用轮询代替固定 sleep
            alive = subprocess.run(["/usr/bin/pgrep", "-f", str(probe / "Contents" / "MacOS" / "Skyrocket")],
                                   capture_output=True, text=True).stdout.strip()
            if alive:
                break
            time.sleep(0.2)
        subprocess.run(["/usr/bin/pkill", "-f", str(probe / "Contents" / "MacOS" / "Skyrocket")],
                       capture_output=True)
        if opener.returncode != 0 or not alive:
            self.skipTest("这台机器没有可用的 LaunchServices（open/pgrep 起不了替身）："
                          "正向路线不判，rc=%s" % opener.returncode)

        # 现场：旧版已装 + 两份陈旧回退包（epoch 比本次运行早）
        now = int(time.time())
        seeded = []
        for back in (90000, 80000):
            d = self.applications / (".Skyrocket.app.rollback-%d" % (now - back))
            d.mkdir()
            seeded.append(d.name)
        stage = self.work / "stage"
        stage.mkdir()
        candidate, cs = self._stub_app(stage, "9.9.9")
        self.assertEqual(cs.returncode, 0, cs.stderr[-200:])
        archive = _zip(candidate, self.work / "ota.zip")
        sha = _sha256(archive)
        target = self.applications / APP_NAME
        result = self.work / "result.json"          # 故意放在安装目录之外：与生产一致
        env = dict(os.environ, TMPDIR=str(self.tmp))
        proc = subprocess.run(["/bin/sh", str(HELPER), "--pid", "2147483646",
                               "--archive", str(archive), "--sha256", sha,
                               "--version", "9.9.9", "--target", str(target),
                               "--result", str(result)],
                              capture_output=True, text=True, env=env, timeout=180)
        self.addCleanup(subprocess.run, ["/usr/bin/pkill", "-f",
                                         str(target / "Contents" / "MacOS" / "Skyrocket")],
                        capture_output=True)
        payload = json.loads(result.read_text(encoding="utf-8")) if result.is_file() else {}
        self.assertEqual(payload.get("status"), "installed",
                         "正向路线没走到底（rc=%s）：%s / %s" % (
                             proc.returncode, payload, (proc.stderr or "")[-200:]))

        left = sorted(p.name for p in self.applications.glob(".Skyrocket.app.rollback-*"))
        self.assertEqual(len(left), 1,
                         "装完之后「应用程序」里只该留最近一份回退包，实际剩下：%s" % left)
        self.assertEqual(sorted(seeded),
                         sorted(p.name for p in self.tmp.glob("sf_update_rollbacks/*")),
                         "被挪走的必须是那两份陈旧的，而且只许挪走不许删")
        plist = (target / "Contents" / "Info.plist").read_text(encoding="utf-8")
        self.assertIn("<string>9.9.9</string>", plist, "装完的还是旧版")
        got = subprocess.run(["/usr/bin/pgrep", "-f", str(target / "Contents" / "MacOS" / "Skyrocket")],
                             capture_output=True, text=True).stdout.strip()
        self.assertTrue(got, "助手说装好了，但按 Info.plist 里的可执行名找不到进程")


class TestHelperVerificationHome(SFCase):
    """真 shell 全流程；只替换系统启动/签名/存活出口，绝不启动候选 GUI。"""

    def setUp(self):
        super().setUp()
        temp = tempfile.TemporaryDirectory(prefix="sf_verify_helper_")
        self.addCleanup(temp.cleanup)
        self.root = pathlib.Path(temp.name).resolve()
        self.home = self.root / "验证 home"
        self.home.mkdir()
        self.apps = self.root / "Applications"
        self.apps.mkdir()
        self.target = _make_app(self.apps, version="1.0.0")
        self.result = self.root / "result.json"
        self.archive = self.root / "ota.zip"
        self.archive.write_bytes(b"not needed before validation")

    def invoke(self, home, target=None, helper=HELPER, extra=(), env_extra=None):
        import hashlib
        args = ["/bin/sh", str(helper), "--pid", "2147483646",
                "--archive", str(self.archive), "--sha256",
                hashlib.sha256(self.archive.read_bytes()).hexdigest(),
                "--version", "9.9.9", "--target", str(target or self.target),
                "--result", str(self.result)]
        if home is not None:
            args += ["--verification-home", str(home)]
        proc = subprocess.run(args + list(extra), capture_output=True, text=True,
                              timeout=30, env=dict(os.environ, TMPDIR=str(self.root),
                                                   **(env_extra or {})))
        payload = json.loads(self.result.read_text()) if self.result.exists() else {}
        return proc, payload

    def test_rejects_unsafe_home_and_target_before_touching_old_app(self):
        other = tempfile.TemporaryDirectory(prefix="sf_verify_other_")
        self.addCleanup(other.cleanup)
        other_target = _make_app(pathlib.Path(other.name))
        link = self.root / "linked-home"
        link.symlink_to(self.home, target_is_directory=True)
        apps_link = self.root / "linked-apps"
        apps_link.symlink_to(self.apps, target_is_directory=True)
        escape = self.root / "escape"
        escape.symlink_to("/Applications", target_is_directory=True)
        outside = ROOT  # 真实存在但不是系统临时目录；不得把 TMPDIR 当作许可根。
        pairs = [("relative", self.target), ("", self.target),
                 (self.root / "missing", self.target), (outside, self.target),
                 (link, self.target), (self.home, other_target),
                 (self.home, apps_link / APP_NAME),
                 (self.home, escape / APP_NAME),
                 (self.home, pathlib.Path("/Applications/Skyrocket.app")),
                 (self.home, self.apps / ".." / "Applications" / APP_NAME)]
        original = (self.target / "Contents/Info.plist").read_bytes()
        for home, target in pairs:
            with self.subTest(home=str(home), target=str(target)):
                proc, payload = self.invoke(home, target)
                self.assertNotEqual(proc.returncode, 0)
                if str(home):
                    self.assertEqual(payload.get("message"), "verification_home_invalid")
                self.assertEqual((self.target / "Contents/Info.plist").read_bytes(), original)
                self.assertEqual(list(self.apps.glob(".*.rollback-*")), [])

    def test_rejects_target_in_a_sibling_dir_even_when_both_share_a_nested_temp_root(self):
        """私有根是 home 的直接父目录；同在更深一层临时目录里的兄弟目录不算同一个根（T-26）。

        旧规则取「临时基目录之后的第一段」当根：TMPDIR 指到更深一层（/tmp/x 或
        $(getconf DARWIN_USER_TEMP_DIR)/x）时，两个不同临时目录共享那一段，跨目录的
        target 就不再被拒。这条用例把两个目录都建在夹具自己的私有根之内，所以与 TMPDIR
        的形状无关；旧规则下 target 落在「第一段」之内会被放行，本用例当场红。
        """
        nest = self.root / "nested-root"
        home = nest / "a" / "验证 home"
        home.mkdir(parents=True)
        other_target = _make_app(nest / "b")
        original = (other_target / "Contents/Info.plist").read_bytes()
        proc, payload = self.invoke(home, other_target)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(payload.get("message"), "verification_home_invalid")
        self.assertEqual((other_target / "Contents/Info.plist").read_bytes(), original)

    def test_rejects_derived_path_symlinks_including_dangling_links(self):
        for name in ("config.json", "universe.json", "last_quote.json",
                     "debug.log", "single.sock"):
            with self.subTest(name=name):
                link = self.home / name
                link.symlink_to(self.root / "missing-external-file")
                proc, payload = self.invoke(self.home)
                self.assertNotEqual(proc.returncode, 0)
                self.assertEqual(payload.get("message"), "verification_home_invalid")
                link.unlink()

    def test_missing_or_duplicate_option_value_is_usage_error(self):
        for extra in (("--verification-home",),
                      ("--verification-home", "")):
            proc, _ = self.invoke(None, extra=extra)
            self.assertEqual(proc.returncode, 64)
        proc, _ = self.invoke(self.home, extra=("--verification-home", str(self.home)))
        self.assertEqual(proc.returncode, 64)

    def test_invalid_pid_is_rejected_before_touching_the_target(self):
        before = (self.target / "Contents/Info.plist").read_bytes()
        proc, payload = self.invoke(self.home, extra=("--pid", "not-a-pid"))
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(payload.get("message"), "invalid_arguments")
        self.assertEqual((self.target / "Contents/Info.plist").read_bytes(), before)

    def instrument_helper(self, fail_first=False):
        """系统命令替身只存在于临时副本；生产脚本不留测试开关。"""
        import shlex
        log = self.root / "open.args"
        opener = self.root / "record-open"
        opener.write_text(
            "#!/bin/sh\n"
            + (f"[ -f {shlex.quote(str(log))} ] && first=0 || first=1\n")
            + f"printf '%s\\0' \"$@\" >> {shlex.quote(str(log))}\n"
            + f"printf '\\0' >> {shlex.quote(str(log))}\n"
            + ("[ \"$first\" -eq 0 ]\n" if fail_first else "exit 0\n"))
        opener.chmod(0o755)
        text = HELPER.read_text(encoding="utf-8")
        text = text.replace("/usr/bin/open", shlex.quote(str(opener)))
        text = text.replace("/usr/bin/codesign", "/usr/bin/true")
        text = text.replace("/usr/bin/pgrep", "/usr/bin/true")
        text = text.replace("set -eu", "set -eu\nsleep() { :; }")
        helper = self.root / "helper.sh"
        helper.write_text(text, encoding="utf-8")
        stage = self.root / "stage"
        stage.mkdir()
        _zip(_make_app(stage), self.archive)
        return helper, log

    def expected_args(self):
        values = {
            "SF_CONFIG_PATH": str(self.home / "config.json"),
            "SF_UNIVERSE_PATH": str(self.home / "universe.json"),
            "SF_LAST_QUOTE_PATH": str(self.home / "last_quote.json"),
            "SF_LOG_PATH": str(self.home / "debug.log"),
            "SF_SINGLE_SOCK": str(self.home / "single.sock"),
            "SF_ALLOW_TEMP_LAUNCH": "1", "QT_QPA_PLATFORM": "offscreen",
            "SF_SEED_FIRST_SCREEN": "0",
        }
        args = ["-n", "-g"]
        for key, value in values.items():
            args.extend(["--env", key + "=" + value])
        return args + [str(self.target)]

    def test_installed_launch_gets_explicit_isolated_environment(self):
        helper, log = self.instrument_helper()
        proc, payload = self.invoke(self.home, helper=helper)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(payload.get("status"), "installed")
        calls = log.read_bytes().split(b"\0\0")[:-1]
        self.assertEqual([c.decode().split("\0") for c in calls], [self.expected_args()])

    def test_failed_restart_and_rollback_launch_use_identical_environment(self):
        helper, log = self.instrument_helper(fail_first=True)
        original = (self.target / "Contents/Info.plist").read_bytes()
        proc, payload = self.invoke(self.home, helper=helper)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(payload, {"status": "rolled_back", "message": "restart_new_app_failed"})
        calls = log.read_bytes().split(b"\0\0")[:-1]
        self.assertEqual([c.decode().split("\0") for c in calls],
                         [self.expected_args(), self.expected_args()])
        self.assertEqual((self.target / "Contents/Info.plist").read_bytes(), original)

    def test_production_launch_keeps_original_open_arguments(self):
        helper, log = self.instrument_helper(fail_first=True)
        _, payload = self.invoke(None, helper=helper)
        self.assertEqual(payload.get("status"), "rolled_back")
        calls = log.read_bytes().split(b"\0\0")[:-1]
        self.assertEqual([c.decode().split("\0") for c in calls],
                         [[str(self.target)], [str(self.target)]])

    def test_production_handoff_reaches_the_replacement_app(self):
        """正式更新也必须把握手令牌交给新版，不能确认超时后回退。"""
        helper, log = self.instrument_helper()
        token = "e" * 32
        self._write_ack(token)
        proc, payload = self.invoke(None, helper=helper, extra=("--handoff", token))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(payload.get("status"), "installed")
        calls = log.read_bytes().split(b"\0\0")[:-1]
        self.assertEqual([c.decode().split("\0") for c in calls],
                         [["-n", "--env", "SF_UPDATE_HANDOFF=" + token, str(self.target)]])

    def _write_ack(self, token, *, version="9.9.9", bundle_id=BUNDLE_ID):
        (self.root / ("handoff-%s.ack.json" % token)).write_text(
            json.dumps({"handoff_id": token, "version": version, "bundle_id": bundle_id},
                       separators=(",", ":")), encoding="utf-8")

    def test_handoff_requires_matching_ack_before_reporting_installed(self):
        helper, _ = self.instrument_helper()
        token = "a" * 32
        self._write_ack(token)
        _, payload = self.invoke(self.home, helper=helper, extra=("--handoff", token))
        self.assertEqual(payload.get("status"), "installed")

    def test_handoff_missing_or_mismatched_ack_rolls_back(self):
        helper, _ = self.instrument_helper()
        for label, token, version, bundle_id in (
                ("missing", "b" * 32, None, None),
                ("wrong-version", "c" * 32, "9.9.8", BUNDLE_ID),
                ("wrong-bundle", "d" * 32, "9.9.9", "com.example.other")):
            with self.subTest(label=label):
                self.target = _make_app(self.apps, version="1.0.0")
                if version is not None:
                    self._write_ack(token, version=version, bundle_id=bundle_id)
                _, payload = self.invoke(self.home, target=self.target, helper=helper,
                                          extra=("--handoff", token))
                self.assertEqual(payload.get("status"), "rolled_back")
                self.assertEqual(payload.get("message"), "startup_confirmation_timeout")

    def test_handoff_token_must_be_exactly_32_lower_hex_characters(self):
        helper, _ = self.instrument_helper()
        # 用户终端是 UTF-8 区域设置，开发机默认没设（走 C 分支）。大写字母是否被 a-f 区间放行
        # 取决于这个设置，所以每个坏令牌在两种区域设置下都必须被拒。
        for locale in ("C", "en_US.UTF-8"):
            for token in ("A" * 32, "e" * 31, "f" * 33, "g" * 32):
                with self.subTest(locale=locale, token=token):
                    self.target = _make_app(self.apps, version="1.0.0")
                    before = (self.target / "Contents" / "Info.plist").read_bytes()
                    _, payload = self.invoke(self.home, target=self.target, helper=helper,
                                             extra=("--handoff", token),
                                             env_extra={"LC_ALL": locale})
                    self.assertEqual(payload.get("status"), "failed")
                    self.assertEqual(payload.get("message"), "handoff_invalid")
                    self.assertEqual((self.target / "Contents" / "Info.plist").read_bytes(), before)
                    self.assertEqual(list(self.apps.glob(".*.rollback-*")), [])


class TestHandoffAcknowledgement(_PositiveRouteSupport, _HelperCase):
    """握手确认的端到端：助手必须等**新版自己写下的 ack**，而不是"进程活着十秒"。

    这条链先前从没被真替身跑过：`--handoff` 之后那段等待循环（含只认 handoff_id、
    版本、bundle id 三个条件）在回归里是空白 —— 而它判断的是"这台机器上到底有没有
    一次成功的更新"，写错方向就是把没起来的新版报成装好了。
    """

    HANDOFF = "0" * 32

    def ack_body(self, handoff=None, version="9.9.9", bundle=BUNDLE_ID):
        payload = {"acknowledged_at": 1.0, "bundle_id": bundle,
                   "handoff_id": handoff or self.HANDOFF, "session_id": "", "version": version}
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    def run_with_handoff(self, handoff=None, ticks=None, ack=None, target=None, version="9.9.9"):
        hid = handoff if handoff is not None else self.HANDOFF
        self._runs = getattr(self, "_runs", 0) + 1
        tag = "%d-%s" % (self._runs, (hid[:6] if hid else "none"))
        stage = self.work / ("stage-%s" % tag)
        stage.mkdir()
        probe, sign = self._stub_app(stage, version, ack=ack)
        if sign.returncode != 0:
            self.skipTest("这台机器不能 ad-hoc 签名（codesign 不可用）：握手路线不判 —— %s"
                          % (sign.stderr or "")[-120:])
        archive = _zip(probe, self.work / ("ota-%s.zip" % tag))
        sha = _sha256(archive)
        target = target or self.applications / APP_NAME
        self.old_plist = (target / "Contents" / "Info.plist").read_bytes()
        new_plist = (probe / "Contents" / "Info.plist").read_bytes()
        result = self.work / "result.json"
        argv = ["/bin/sh", str(HELPER), "--pid", "2147483646", "--archive", str(archive),
                "--sha256", sha, "--version", version, "--target", str(target),
                "--result", str(result)]
        if hid:
            argv += ["--handoff", hid]
        env = dict(os.environ, TMPDIR=str(self.tmp))
        if ticks is not None:
            env["SF_HANDOFF_ACK_TICKS"] = str(ticks)
        proc = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=180,
                              errors="replace")
        self.addCleanup(subprocess.run, ["/usr/bin/pkill", "-f",
                                         str(probe / "Contents" / "MacOS" / "Skyrocket")],
                        capture_output=True)
        self.addCleanup(subprocess.run, ["/usr/bin/pkill", "-f",
                                         str(target / "Contents" / "MacOS" / "Skyrocket")],
                        capture_output=True)
        payload = json.loads(result.read_text(encoding="utf-8")) if result.is_file() else {}
        return payload, proc, new_plist

    def test_a_matching_ack_completes_the_install(self):
        path = self.work / ("handoff-%s.ack.json" % self.HANDOFF)
        payload, _proc, new_plist = self.run_with_handoff(ack=(path, self.ack_body()))
        self.assertEqual(payload.get("status"), "installed", payload)
        self.assertTrue(path.is_file(), "ack 就是这次更新确认过的凭据，不许被顺手清掉")
        installed = (self.applications / APP_NAME / "Contents" / "Info.plist").read_bytes()
        self.assertEqual(installed, new_plist, "确认成功之后「应用程序」里必须是这次装上去的那一份")

    def test_a_missing_ack_rolls_back_instead_of_claiming_success(self):
        payload, _proc, _new = self.run_with_handoff(ticks=4)
        self.assertEqual(payload.get("message"), "startup_confirmation_timeout", payload)
        restored = (self.applications / APP_NAME / "Contents" / "Info.plist").read_bytes()
        self.assertEqual(restored, self.old_plist,
                         "确认超时之后必须回到原来那一份，用户手上还得有一个能用的程序")

    def test_an_ack_for_another_version_does_not_count(self):
        path = self.work / ("handoff-%s.ack.json" % self.HANDOFF)
        payload, _proc, _new = self.run_with_handoff(ticks=4, ack=(path, self.ack_body(version="8.8.8")))
        self.assertEqual(payload.get("message"), "startup_confirmation_timeout", payload)

    def test_an_ack_from_another_bundle_does_not_count(self):
        path = self.work / ("handoff-%s.ack.json" % self.HANDOFF)
        payload, _proc, _new = self.run_with_handoff(ticks=4, ack=(path, self.ack_body(bundle="com.other.app")))
        self.assertEqual(payload.get("message"), "startup_confirmation_timeout", payload)

    def test_an_ack_for_another_handoff_does_not_count(self):
        path = self.work / ("handoff-%s.ack.json" % self.HANDOFF)
        payload, _proc, _new = self.run_with_handoff(ticks=4, ack=(path, self.ack_body(handoff="f" * 32)))
        self.assertEqual(payload.get("message"), "startup_confirmation_timeout", payload)

    def test_a_non_numeric_tick_count_falls_back_to_the_default(self):
        """拍数只认正整数：写错成字母或 0 也必须按默认值跑，不能变成"永远等不到"或语法错误。"""
        path = self.work / ("handoff-%s.ack.json" % self.HANDOFF)
        for junk in ("abc", "0", ""):
            payload, _proc, _new = self.run_with_handoff(ticks=junk, ack=(path, self.ack_body()))
            self.assertEqual(payload.get("status"), "installed", "%r: %s" % (junk, payload))

    def test_a_malformed_handoff_identifier_is_refused_before_anything_moves(self):
        for bad in ("zz" * 16, "0" * 31, "0" * 33):
            payload, _proc, _new = self.run_with_handoff(handoff=bad)
            self.assertEqual(payload.get("message"), "handoff_invalid", "%s: %s" % (bad, payload))


if __name__ == "__main__":
    import unittest
    unittest.main(verbosity=2)
