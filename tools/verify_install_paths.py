#!/usr/bin/env python3
"""首装与更新四条路线的实测核验（v1.4.58 起）。

它守的是一句此前只靠嘴说的承诺：**"用户照指引装完，第一次就能打开"**。
macOS 拦不拦一个 app，取决于文件上有没有 `com.apple.quarantine`（下称"来历标签"），
而标签是**下载这个动作**贴的：浏览器/微信贴，程序自己的 urllib 不贴。
这条机制决定了几件事，都必须量而不是猜：

  R0 名字同源 —— 产品名的唯一来源是 sf_constants.APP_DISPLAY_NAME；更新助手与安装脚本
                里写死的两代顶层目录名必须包含它（v1.4.55 改名漏掉一处，装机版助手实测
                对线上 1.4.57 资产必失败 archive_layout_invalid，自动更新自改名起从未成功过）。
  R1 curl 一行 —— 用 curl 取回的包**天生不带标签**，装完直接能开。
  R2 DMG 内双击 —— 卷里的 .command 不带标签（所以能双击、能执行），它拷完自己清标签；
                这一条还看图标：卷里那枚脚本必须带着本轮画出来的图标，忘了贴就判红；
                源包**内层文件**上贴一枚标签，清完必须递归读回为空（只看包根的核验是瞎的）。
  R2b 老用户升级 —— 目标目录里已经有旧版：装完旧版必须在废纸篓、新版必须在位。
                这条是 2026-10-13 补的：R2 原先永远装进**空目录**，于是对外说明里写给
                老用户的主用途「有旧版 → 移废纸篓 → 装新版」从来没被执行过一次，
                而那条分支里藏着一枚必现崩溃（见下面"站在用户那一侧"）。
  R2c 旧版正在运行 —— 装之前必须停下来等它退出，**绝不**在旧版还跑着的时候报"装好了"。
                实测过的手场：旧版在跑时点「一键安装」，脚本一路 rc=0 打「正在打开」，
                而机器上活着的仍是旧一代载荷，新一代被自家多开守卫 sys.exit(0) 静默退出，
                被移进废纸篓的旧版**仍在运行** —— 用户看到的就是"装了新版问题怎么还在"。
  R2d 源包是坏的 —— 校验必须排在**移动旧版之前**：坏包只能得到一句"什么都没动"，
                不能得到「应用程序」里一份坏包 + 废纸篓里一份好旧版。
  R2e 标签清不掉 —— 包内某个文件带 uchg（immutable）时 `xattr -dr` 清不动它，
                脚本必须如实报失败并把旧版放回原位，而不是说一句"标签已清"。
  R3 拖拽 + 放行 —— 复现缺陷现场：从带标签的 DMG 里拖出来的 app **确实带标签**，
                现行 FR-12 那条命令确实能清掉（对照用，证明"墙"是真的、也是能过的）。
  R4 更新助手 —— 助手必须同时接受改名前/改名后两种包内顶层名。

**站在用户那一侧，不是站在开发机这一侧**：所有跑安装脚本的路线都强制用 UTF-8 locale
（`LANG=zh_CN.UTF-8`，取本机 `locale -a` 里真存在的那一枚）。这不是风格：
macOS 自带的 `/bin/sh`（bash 3.2.57）在 UTF-8 locale 下会把紧跟变量的中文标点首字节
吞进变量名，`set -u` 当场以「unbound variable」终止；而这台开发机的 shell **没有设 LANG**，
于是走的是安全一侧 —— 用户终端是 `LANG=zh_CN.UTF-8`（AppleLocale=zh_CN），走的是必炸那一侧。
判据本身也要能被拆掉：S10 把关卡退回 C locale，那条路线必须红（"关卡不测这一侧"= 没有这条判据）。
`chflags uchg` 与 `com.apple.quarantine` **都能活着穿过 ditto 与 HFS+/UDZO 卷**（实测），
所以 R2e 的现场可以在造卷之前就种好，而不是装完之后补。

判据只看可观测事实：目标路径上标签在不在、脚本退出码、废纸篓里有什么、进程在不在、
助手写回的 result.json。
不启动任何真程序（安装脚本走 SF_INSTALL_NO_LAUNCH=1；R4 用未签名替身，助手会在
codesign 那步停下，永远走不到 open；R2c 用最小可启动替身 LSUIElement + /bin/sh 跑 sleep，
既不在用户屏幕开窗口也不碰 /Applications），全程临时目录。

用法
    ./.venv/bin/python tools/verify_install_paths.py            # 跑全部路线
    ./.venv/bin/python tools/verify_install_paths.py --self-test # 逐处种错，必须全被抓到

    ⚠ 默认档（上面那九条路线）里有六条要复制"本机装好的真产物"。换一台没装过的机器
    （云端复检机就是）它们跑不起来，会被如实记成"没跑"并大声 WARNING ——
    既不算通过也不算失败。一条都没跑起来时拒绝给结论（退 1），免得"没有输入"被读成"全过"。
    ⚠ 变异自检档**不需要**真产物：种错验的是判据形状（图标、显示名、标签递归、codesign
    有没有牙、locale），几 KB 的签名替身全覆盖得到，所以 15 处全部并发跑完 ≈19 秒
    （串行 48 秒会撞 `tests/test_release_gates.py` 那条 60 秒单用例上限）。
    两条口径别混：默认档宣称的是"用户拿到的那一份"，自检档宣称的是"判据抓得住错"。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import apply_installer_icon as AIC  # noqa: E402  （图标的贴法与判据只有一份实现）

from sf_constants import (  # noqa: E402  （名字唯一来源，见 R0）
    APP_DISPLAY_NAME,
    APP_VERSION,
    LEGACY_APP_DISPLAY_NAMES,
)

HELPER = ROOT / "updater" / "update_helper.sh"
BUNDLE_ID = "com.skyrocket.app"
QUARANTINE = "com.apple.quarantine"
XATTR = "/usr/bin/xattr"
CURRENT_APP_NAME = "%s.app" % APP_DISPLAY_NAME
LEGACY_APP_NAMES = ["%s.app" % n for n in LEGACY_APP_DISPLAY_NAMES]

# 用户终端那侧的 locale（AppleLocale=zh_CN → LANG=zh_CN.UTF-8）。按可用性依次取，
# 一个都不存在时相关路线如实 skip —— 站在 C locale 跑这一族判据是没有意义的。
USER_LANGS = ("zh_CN.UTF-8", "en_US.UTF-8")
STANDIN_BUNDLE_ID = "com.sfprobe.verifyinstallpaths"


def user_lang() -> str | None:
    """本机 `locale -a` 里真存在的那个 UTF-8 locale；取不到返回 None。"""
    try:
        p = subprocess.run(["/usr/bin/locale", "-a"], capture_output=True,
                           text=True, errors="replace", timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    available = set(p.stdout.split())
    for cand in USER_LANGS:
        if cand in available:
            return cand
    return None


def route_home(work: Path, target_name: str) -> Path:
    """每条路线自己的 HOME（里面才有 .Trash 与 Logs）。

    绝不能几条路线共用一个 HOME：R2c 与 R2d 的判据是「废纸篓必须是空的」
    （什么都没动），共用时上一条路线刚移进去的旧版会让它判红 —— 那是夹具串味，不是缺陷。
    """
    return work / ("home_" + target_name)


def install_env(work: Path, target_name: str, lang: str | None = "auto",
                **overrides) -> dict:
    """跑安装脚本时的环境：**始终带 UTF-8 locale**，外加各条路线自己的开关。

    HOME 一律指向临时目录：脚本会把旧版送进 `$HOME/.Trash`，那是判据要看的地方，
    绝不能是用户真实的废纸篓。
    """
    home = route_home(work, target_name)
    (home / ".Trash").mkdir(parents=True, exist_ok=True)
    if lang == "auto":
        lang = user_lang()
    env = {"HOME": str(home), "SF_INSTALL_NO_LAUNCH": "1"}
    if lang:
        env["LANG"] = lang
        env["LC_ALL"] = lang
        env["LC_CTYPE"] = lang
    env.update({k: str(v) for k, v in overrides.items() if v is not None})
    return env


def has_quarantine_anywhere(path: Path) -> tuple[bool, str]:
    """递归读回 xattr：标签藏在包内某个文件上时，只看包根的核验是瞎的。"""
    rc, out = run([XATTR, "-rl", str(path)])
    if QUARANTINE in out:
        hit = next((ln for ln in out.splitlines() if QUARANTINE in ln), "")
        return True, hit
    return False, ""


def make_immutable(path: Path) -> None:
    """给一个文件上 uchg：`xattr -dr` 清不动它（实测 EPERM），而读与拷贝都不受影响。

    实测两件事一起成立，这条路线才做得出来：**`com.apple.quarantine` 与 `uchg` 都能
    活着穿过 ditto 与 HFS+/UDZO 卷** —— 所以现场在造卷之前种好，装完之后依然在。
    """
    rc, out = run(["/usr/bin/chflags", "uchg", str(path)])
    if rc != 0:
        raise NoFixture("上不了 uchg（这条判据的前提没了）: " + out[-160:])


def clear_immutable(root: Path) -> None:
    """临时目录里有 immutable 文件就删不掉，收尾必须先递归卸掉。"""
    if root.is_dir():
        run(["/usr/bin/chflags", "-R", "nouchg", str(root)])


def launch(app: Path) -> tuple[int, str]:
    """真拉起一个最小可启动替身（LSUIElement + /bin/sh 跑 sleep），用于"旧版正在运行"。"""
    return run(["/usr/bin/open", str(app)])


def kill_by_path(fragment: str) -> None:
    run(["/usr/bin/pkill", "-f", fragment])



def launcher_script(root: Path = ROOT) -> Path:
    """双击安装脚本：名字**不写死**，唯一来源是 installer/ 里那一枚 *.command。

    2026-10-12 把脚本从 Install-Skyrocket.command 改成「一键安装.command」时定的规矩：
    文件名只有一个，引用它的地方有三处（发版脚本、本工具、对外安装说明）。写死任何一处，
    改名时漏掉的那一处**不会报错**，只会让卷里安静地少掉一键安装 —— 用户撞上的是
    "拖完被 Gatekeeper 拦"那面墙，而这道核验当时照样全绿（它找的是旧名字，找不到就打
    "DMG 里没有 X"，可 DMG 是它自己造的，永远带着它写死的那个名字）。
    数量必须恰好一枚：0 枚=脚本没了，2 枚=有人复制了一份，两种都不该出包。
    """
    found = sorted(root.glob("installer/*.command"))
    if len(found) != 1:
        raise RuntimeError(
            "installer/ 下应当恰好有一个 *.command，实际 %d 个（%s）"
            % (len(found), "、".join(p.name for p in found) or "一个都没有"))
    return found[0]


class NoFixture(RuntimeError):
    """前提不成立（比如本机没有真产物可复制）—— 如实跳过并打印理由，绝不假装通过。"""


def run(cmd, env=None, timeout=240):
    """统一出口：返回 (rc, stdout+stderr)。命令坏在哪儿要能被打印出来。

    errors="replace" 是必需的：助手一旦被 awk 之类"吐出非法 UTF-8 再非零退出"的缺陷
    击中（本轮 S6 种的就是这个），严格解码会在**核验器**里抛 UnicodeDecodeError ——
    于是这道闸门不是红、而是崩，看起来像工具坏了，说不清被测对象到底哪儿不对。
    """
    merged = dict(os.environ)
    if env:
        merged.update(env)
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                           env=merged, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except OSError as exc:
        return 127, "无法执行 %s: %s" % (cmd[0], exc)
    except subprocess.TimeoutExpired:
        return 124, "超时（%ss）: %s" % (timeout, cmd[0])


def has_quarantine(path: Path) -> bool:
    rc, out = run([XATTR, "-l", str(path)])
    return rc == 0 and QUARANTINE in out


def stamp_quarantine(path: Path, agent: str = "Safari") -> None:
    """手工贴标签，模拟「用户用浏览器把它下载到了这台机器上」。"""
    rc, out = run([XATTR, "-w", QUARANTINE,
                   "0082;%x;%s;%s" % (0x68C3F0A1, agent, uuid.uuid4().hex.upper()),
                   str(path)])
    if rc != 0:
        raise NoFixture("贴不上标签（判据的前提没了）: " + out)


def real_app() -> Path:
    """给 R2/R3 用的真产物：必须是**签过名**的，否则安装脚本里的 codesign 校验
    会以"包不完整"停下，测出来的是夹具而非路线。"""
    for cand in (Path("/Applications") / CURRENT_APP_NAME, ROOT / "dist" / CURRENT_APP_NAME):
        if cand.is_dir():
            return cand
    raise NoFixture("本机没有可复制的真产物（/Applications 与 dist/ 里都没有 %s），"
                    "R2/R3 需要它才能过 codesign 校验" % CURRENT_APP_NAME)


def make_fake_app(parent: Path, name: str, version: str = APP_VERSION,
                  bundle_id: str = BUNDLE_ID) -> Path:
    """最小可辨识的 .app 替身，**故意不签名**。

    为什么故意不签：codesign --verify 失败会让更新助手在替换动作之前就停下，
    于是这条路线永远走不到 `open` —— 不在用户屏幕上拉起任何窗口，也就能反复跑。
    R4 要守的是"助手停在哪儿"（解包/命名这一关过没过），停在签名步正好证明过了。
    """
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
        '</dict></plist>\n' % (bundle_id, version, stem, stem),
        encoding="utf-8")
    (app / "Contents" / "MacOS" / stem).write_bytes(b"\0\0\0\0")
    return app


def make_launchable_app(parent: Path, name: str, version: str, marker: str,
                    bundle_id: str = STANDIN_BUNDLE_ID) -> Path | None:
    """**可启动**的最小替身：ad-hoc 签名 + LSUIElement + `/bin/sh` 里跑 `sleep <marker>`。

    与 make_fake_app 分开是有意的：R4 的替身必须**不签名**（助手要停在 codesign 那一站），
    而 R2c 要的是"机器上真有一份在运行"。载荷用 sleep 而不是 exec —— exec 之后 argv 变成
    /bin/sleep，按包路径找进程的判据就认不出它（同 tests/test_update_helper.py 的口径）。
    marker 是唯一的分辨手段：两代替身**路径完全相同**（同一个 bundle 名），argv 长得一样，
    只有 sleep 的参数能回答"活着的到底是哪一代"。

    返回 None = 这台机器签不了名（codesign 不可用），调用方如实 skip。
    """
    app = make_fake_app(parent, name, version=version, bundle_id=bundle_id)
    exe = app / "Contents" / "MacOS" / name[:-4]
    exe.write_text("#!/bin/sh\n/bin/sleep %s\n" % marker, encoding="utf-8")
    exe.chmod(0o755)
    rc, out = run(["/usr/bin/codesign", "-s", "-", "--force", str(app)])
    if rc != 0:
        return None
    return app


def light_signed_app(work: Path, tag: str) -> Path:
    """几 KB 的**签名替身**，用来替代 71MB 真产物造卷 —— 只给变异自检用。

    为什么需要：`tests/test_release_gates.py::test_every_chain_tool_can_prove_it_fails`
    会在一个 60 秒上限的用例里跑本工具的 `--self-test`。自检从 7 处种错涨到 15 处，
    每处都要 ditto 一份真产物再造 HFS+ 卷（实测单枚 9.1 秒），整轮自检 52 秒起步，
    撞的就是那条上限（第一次撞上时用例被 0:01:00 掐掉）。
    判据本身不需要 71MB：这里种错要验的是**图标、显示名、可执行位、标签递归读回、
    codesign 有没有牙、locale**，替身全都覆盖到（替身签名过 → codesign 判"完整"，
    改掉它 → 判"被修改"，两向都在 tests/test_installer_script_branches.py 实测过）。
    默认档（发版链 3.8 跑的那五条）继续用真产物 —— 那一条宣称的是"用户拿到的那一份"。
    """
    d = work / ("light-" + tag)
    d.mkdir(parents=True, exist_ok=True)
    app = make_launchable_app(d, CURRENT_APP_NAME, APP_VERSION, marker="1",
                              bundle_id=BUNDLE_ID)
    if app is None:
        raise NoFixture("这台机器不能 ad-hoc 签名，轻夹具造不出来（这条变异跑不了）")
    return app


def zip_from(src_app: Path, out: Path) -> Path:
    """--keepParent 是必须的：不带它，zip 顶层就是 Contents/，
    助手的"顶层必须是一个 .app"检查会红 —— 那是夹具的错，不是被测对象的错。"""
    rc, msg = run(["/usr/bin/ditto", "-c", "-k", "--keepParent", str(src_app), str(out)])
    if rc != 0:
        raise NoFixture("造 zip 失败: " + msg[-300:])
    return out


def sha256_of(path: Path) -> str:
    rc, out = run(["/usr/bin/shasum", "-a", "256", str(path)])
    if rc != 0:
        raise NoFixture("算哈希失败: " + out)
    return out.split()[0]


# ---------------------------------------------------------------- 五条核验

def check_name_source(work: Path | None = None,
                    helper_text: str | None = None) -> tuple[bool, str]:
    """`work` 只为了让路线表里每一条的形状都是 `(work) -> (通过?, 说明)` ——
    调用方按名字批量打桩时不必再记"哪一条不收参数"。"""
    """R0：助手与安装脚本里写死的顶层目录名，必须同时钉住当前名与旧名两代。

    这里读的确实是源码文本，但守的不是书写形态，而是**两个产物必须同源**：
    名字的唯一来源是 sf_constants.APP_DISPLAY_NAME，而 shell 脚本拿不到 Python 常量，
    只能写死；写死就会漂移，漂移的后果是自动更新静默失败（v1.4.55 真实踩过一次）。
    """
    missing = []
    launcher = launcher_script()
    targets = (("updater/update_helper.sh", HELPER),
               ("installer/%s" % launcher.name, launcher))
    for label, path in targets:
        if not path.is_file():
            return False, "缺文件 %s" % path
        body = helper_text if (helper_text is not None and label.endswith("update_helper.sh")) \
            else path.read_text(encoding="utf-8")
        tokens = set(body.replace('"', " ").split())
        for name in [CURRENT_APP_NAME] + LEGACY_APP_NAMES:
            if name not in tokens:
                missing.append("%s 里没钉住 %s" % (label, name))
    if missing:
        return False, "；".join(missing)
    return True, "两代顶层名都在：current=%s, legacy=%s" % (
        CURRENT_APP_NAME, ", ".join(LEGACY_APP_NAMES))


def check_curl_route(work: Path, stamp_download: bool = False,
                     stamp_installed: bool = False) -> tuple[bool, str]:
    """R1：curl 取回的包不带标签 → 解压出来的 app 也不带 → 第一次就能开。

    两个开关只给 --self-test 用：在那里贴一个假标签，看这条判据会不会红。
    """
    d = work / "r1"
    d.mkdir(parents=True, exist_ok=True)
    app = make_fake_app(d, CURRENT_APP_NAME)
    archive = zip_from(app, d / "pkg.zip")
    fetched = d / "downloaded.zip"
    rc, msg = run(["/usr/bin/curl", "-fsSL", "--max-time", "60",
                   "-o", str(fetched), "file://%s" % archive])
    if rc != 0 or not fetched.is_file():
        return False, "curl 取包失败 rc=%s %s" % (rc, msg[-200:])
    if stamp_download:
        stamp_quarantine(fetched)
    if has_quarantine(fetched):
        return False, "curl 下载的文件竟然带了来历标签 —— 这条路线的前提不再成立（机制变了）"
    dest = d / "Applications"
    dest.mkdir(exist_ok=True)
    rc, msg = run(["/usr/bin/ditto", "-x", "-k", str(fetched), str(dest)])
    if rc != 0:
        return False, "ditto 解压失败: " + msg[-200:]
    installed = dest / CURRENT_APP_NAME
    if not installed.is_dir():
        return False, "解压后没看到 app 本体（顶层结构：%s）" % ", ".join(sorted(os.listdir(dest)))
    if stamp_installed:
        stamp_quarantine(installed)
    if has_quarantine(installed):
        return False, "装出来的 app 带标签，用户第一次打开会被拦"
    return True, "curl → 解压 → 装完全程无标签（零放行）"


def build_user_dmg(work: Path, tag: str, stamp_script: bool = False,
                   with_icon: bool = True, hide_ext: bool = True,
                   script_text: str | None = None,
                   inner_tag: str | None = None,
                   corrupt_payload: bool = False,
                   fixture: str = "real") -> tuple[Path, Path]:
    """造一份「用户用浏览器下载到」的 DMG：卷里有真产物与双击脚本，DMG 本身贴标签。

    `with_icon` / `hide_ext` 只给变异自检用（S7 忘了贴图标、S8 忘了隐藏 `.command` 后缀
    —— 发版脚本里那一步没跑时，这条路线必须红）。
    `script_text` 给变异自检用：把安装脚本换成种过错的版本（例如核验只看包根）。
    `inner_tag` 给 R2/R2e 用：在**包内一个文件**上贴来历标签，
      · "removable" = 正常能清掉的，脚本必须递归清干净；
      · "immutable" = 再上一枚 uchg，`xattr -dr` 清不动它，脚本必须如实报失败。
    `corrupt_payload` 给 R2d 用：改掉包内一个已被签名覆盖的字段，让 codesign 必判失败
      （实测 `invalid signature (code or signature have been modified)`）。
    `fixture="light"` 只给变异自检用：用几 KB 的签名替身代替 71MB 真产物（见
      `light_signed_app` 的注释 —— 15 处种错各造一枚真卷会撞上用例的 60 秒上限）。
    """
    d = work / ("dmg-" + tag)
    payload = d / "payload"
    payload.mkdir(parents=True, exist_ok=True)
    if fixture == "light":
        src = light_signed_app(d, tag)
    elif fixture == "real":
        src = real_app()
    else:
        raise NoFixture("不认识的 fixture：%s" % fixture)
    app = payload / CURRENT_APP_NAME
    rc, msg = run(["/usr/bin/ditto", str(src), str(app)])
    if rc != 0:
        raise NoFixture("复制真产物失败: " + msg[-200:])
    if corrupt_payload:
        plist = app / "Contents" / "Info.plist"
        body = plist.read_text(encoding="utf-8")
        if "APPL" not in body:
            raise NoFixture("真产物的 Info.plist 里没有 APPL，坏包现场种不出来")
        plist.write_text(body.replace("APPL", "ZZZZ", 1), encoding="utf-8")
    launcher = launcher_script()
    if script_text is None:
        shutil.copyfile(launcher, payload / launcher.name)
    else:
        (payload / launcher.name).write_text(script_text, encoding="utf-8")
    os.chmod(payload / launcher.name, 0o755)
    if inner_tag:
        inner = app / "Contents" / "Info.plist"
        if not inner.is_file():
            raise NoFixture("真产物里没有 %s，这条判据的前提没了" % inner)
        stamp_quarantine(inner)
        if inner_tag == "immutable":
            make_immutable(inner)
        elif inner_tag != "removable":
            raise NoFixture("不认识的 inner_tag：%s" % inner_tag)
    if with_icon:
        # 与 build_release.sh 第 4 步同一步动作：外观是发版链贴上去的，夹具不贴就等于
        # 测了一个产品里不存在的形状
        AIC.apply_icon(payload / launcher.name)
    if hide_ext:
        # 顺序照发版链：图标在前、隐藏后缀在后（两者写的是同一条元数据的同一格 flags）
        AIC.hide_extension(payload / launcher.name)
    if stamp_script:
        # 必须在 hdiutil 造卷之前贴：贴完再打镜像，才等于"这个脚本本身被下载过"。
        stamp_quarantine(payload / launcher.name)
    image = d / "user.dmg"
    vol = "SFPROBE_%s" % uuid.uuid4().hex[:8]
    rc, msg = run(["/usr/bin/hdiutil", "create", "-ov", "-volname", vol,
                   "-fs", "HFS+", "-srcfolder", str(payload),
                   "-format", "UDZO", str(image)])
    if rc != 0:
        raise NoFixture("造 DMG 失败（这条核验的前提就没了）: " + msg[-300:])
    stamp_quarantine(image)
    return image, app


def mount(image: Path, at: Path) -> None:
    at.mkdir(parents=True, exist_ok=True)
    rc, msg = run(["/usr/bin/hdiutil", "attach", "-nobrowse", "-readonly",
                   "-mountpoint", str(at), str(image)])
    if rc != 0:
        raise NoFixture("挂载失败: " + msg[-300:])


def unmount(at: Path) -> None:
    if at.exists():
        run(["/usr/bin/hdiutil", "detach", "-force", str(at)])


def installed_version(app: Path) -> str:
    """读一份 bundle 的版本 —— 判据要能分辨"原位躺着的是旧的那一份还是新拷的那一份"。"""
    rc, out = run(["/usr/libexec/PlistBuddy", "-c", "Print :CFBundleShortVersionString",
                   str(app / "Contents" / "Info.plist")])
    return out.strip() if rc == 0 else ""


def run_from_volume(script: Path, work: Path, target_dir: Path, lang: str | None = "auto",
                    **overrides) -> tuple[int, str, str]:
    """在**用户那一侧的 locale** 下真跑一次卷内安装脚本，返回 (rc, 输出, 用掉的 locale)。"""
    env = install_env(work, target_dir.name, lang=lang,
                        SF_INSTALL_DIR=str(target_dir), **overrides)
    used = env.get("LANG") or ""
    rc, msg = run(["/bin/sh", str(script)], env=env)
    return rc, msg, used


def locale_guard(lang: str) -> tuple[bool, str]:
    """关卡自己也要能被识破：退回 C locale 就等于不测这一族缺陷。"""
    if not lang:
        return False, "本机没有 UTF-8 locale，站在安全一侧跑这条判据没有意义"
    if "UTF-8" not in lang.upper():
        return False, ("核验跑在 %s 下 —— 用户终端是 UTF-8 locale，"
                       "而 bash 3.2 只在 UTF-8 下会把中文标点吞进变量名（这一族缺陷看不见）" % lang)
    return True, ""


def volume_script(mnt: Path, work: Path, target_name: str,
                  old_version: str | None = None) -> tuple[Path, Path]:
    """挂出来的卷里那枚脚本，加上一个（可选）预置了旧版的安装目录。"""
    script = mnt / launcher_script().name
    if not script.is_file():
        raise NoFixture("DMG 里没有 %s（build_release.sh 第 4 步没把它放进去）" % script.name)
    if not (script.stat().st_mode & 0o111):
        raise NoFixture("卷内脚本丢了可执行位，双击只会打开文本编辑器")
    target_dir = work / target_name
    target_dir.mkdir(parents=True, exist_ok=True)
    if old_version is not None:
        make_fake_app(target_dir, CURRENT_APP_NAME, version=old_version)
    return script, target_dir


def check_dmg_launcher_route(work: Path, stamp_script: bool = False,
                             with_icon: bool = True, hide_ext: bool = True,
                             script_text: str | None = None,
                             inner_tag: str | None = "removable",
                             force_lang: str | None = None,
                             fixture: str = "real",
                             check_appearance: bool = True) -> tuple[bool, str]:
    """R2：带标签的 DMG 里，脚本本身不带标签（所以能双击）；跑完装出的 app 递归读回无标签。

    2026-10-12 起这条还看**卷里那条目长什么样**：图标必须是本轮画出来的图标，
    显示名必须已经去掉 `.command`（用户看到的是「一键安装」）。
    为什么归到这里而不是新写一条路线：判据要的是"挂出来的卷里那个文件长什么样"，
    而 R2 本来就是唯一真挂卷、真读卷内条目的那一条。

    2026-10-13 又加了两件事，因为原来的写法"装完包根没标签"根本测不到递归与否：
      · 源包**内层文件**先贴一枚标签（`inner_tag="removable"`），装完必须递归读回为空；
      · 全程用用户那一侧的 UTF-8 locale（`force_lang` 只给变异自检用）。
    """
    image, _ = build_user_dmg(work, "r2", stamp_script=stamp_script,
                              with_icon=with_icon and check_appearance,
                              hide_ext=hide_ext and check_appearance,
                              script_text=script_text, inner_tag=inner_tag,
                              fixture=fixture)
    lang = "C" if force_lang == "C" else user_lang()
    ok, why = locale_guard(lang or "")
    if not ok:
        return False, why
    mnt = work / "r2mnt"
    try:
        mount(image, mnt)
        script, target_dir = volume_script(mnt, work, "r2apps")
        if check_appearance:
            look_ok, look_note = AIC.check(script)
            if not look_ok:
                return False, ("卷内脚本的外观不对（用户看到的仍是一页写着 COMMAND 的缩略图，"
                               "或仍带着 .command 后缀）：" + look_note)
        if has_quarantine(script):
            return False, "卷内脚本被贴了标签 —— 双击会被 Gatekeeper 拦，这条路线不成立"
        # 这一条不测"旧版在跑"（R2c 才是）：本机上「应用程序」里那份与临时目录里那份
        # 在 pgrep 的名字口径下长得一样，所以这里显式声明跳过那一步。
        rc, msg, _ = run_from_volume(script, work, target_dir, lang=lang,
                                     SF_INSTALL_ALLOW_RUNNING="1")
        installed = target_dir / CURRENT_APP_NAME
        if rc != 0:
            return False, "安装脚本 rc=%s：%s" % (rc, msg[-320:])
        if not installed.is_dir():
            return False, "脚本退出 0 却没装上任何东西（假绿）\n" + msg[-320:]
        still, where = has_quarantine_anywhere(installed)
        if still:
            return False, ("标签没被递归清干净，用户还是要多一步（残留在 %s）\n"
                           "而脚本退出码是 0 —— 它把自己那句「标签已清」说成了假话：%s"
                           % (where[:160], msg[-200:]))
        logs = sorted((route_home(work, "r2apps") / "Library" / "Logs").glob("*.log"))
        if not logs:
            return False, "装完了却没有安装日志（用户卡住时无从发文件，只能截图）"
        return True, "卷内脚本无标签可双击；装完递归读回无标签；日志已落盘（locale=%s）" % lang
    finally:
        unmount(mnt)


def check_upgrade_route(work: Path, fixture: str = "real") -> tuple[bool, str]:
    """R2b：目标目录里已经有旧版（老用户升级，对外说明里写给老用户的那一条路）。

    这条是 2026-10-13 补的。R2 原先永远把安装目录建成**空目录**，于是脚本里
    「有旧版 → 移废纸篓 → 装新版」这整段从没被执行过，而那一段里正藏着一枚
    UTF-8 locale 下的必现崩溃（`$DEST，`）与"校验排在破坏之后"的顺序缺陷。
    """
    image, _ = build_user_dmg(work, "r2b", fixture=fixture)
    lang = user_lang()
    ok, why = locale_guard(lang or "")
    if not ok:
        return False, why
    mnt = work / "r2bmnt"
    try:
        mount(image, mnt)
        script, target_dir = volume_script(mnt, work, "r2bapps", old_version="0.0.1")
        rc, msg, _ = run_from_volume(script, work, target_dir, lang=lang,
                                     SF_INSTALL_ALLOW_RUNNING="1")
        if rc != 0:
            return False, "老用户升级这条路线 rc=%s：%s" % (rc, msg[-320:])
        if installed_version(target_dir / CURRENT_APP_NAME) == "0.0.1":
            return False, "旧版还在原位，新版没装上：\n" + msg[-320:]
        trash = sorted(p.name for p in (route_home(work, "r2bapps") / ".Trash").glob("*"))
        if not trash or not any(n.endswith(".old") for n in trash):
            return False, "旧版没有整份移到废纸篓（废纸篓里是 %s）" % (trash or "空的")
        if installed_version(target_dir / CURRENT_APP_NAME) != installed_version(image.parent / "payload" / CURRENT_APP_NAME):
            return False, "装上的版本与卷里那份不是同一份"
        return True, "旧版移入废纸篓（未删除）、新版在位且与卷内同源（locale=%s）" % lang
    finally:
        unmount(mnt)


def check_running_instance_route(work: Path, fixture: str = "real") -> tuple[bool, str]:
    """R2c：旧版**正在运行**时必须停下来等，不许在旧版还跑着的时候报"装好了"。

    这一条对着的是实测出来的手场：旧版在跑时双击「一键安装」，脚本一路 rc=0 打
    「正在打开 Skyrocket……」，而机器上活着的仍是旧一代载荷 —— 新一代被自家多开守卫
    `sys.exit(0)` 静默退出（`sf_logging._become_single_instance` /
    `stock_float_window.py` 的入口），被移进废纸篓的旧版**仍在运行**。
    用户看到的就是"按指引装了新版，问题怎么还在"。
    只等不杀：与更新助手同一条理由，强杀会截断「退出时保存配置」。
    """
    image, _ = build_user_dmg(work, "r2c", fixture=fixture)
    lang = user_lang()
    ok, why = locale_guard(lang or "")
    if not ok:
        return False, why
    marker = "6%s" % (abs(hash(str(work))) % 10 ** 5)
    mnt = work / "r2cmnt"
    try:
        mount(image, mnt)
        script, target_dir = volume_script(mnt, work, "r2capps")
        old = make_launchable_app(target_dir, CURRENT_APP_NAME, "0.0.1", marker)
        if old is None:
            raise NoFixture("这台机器不能 ad-hoc 签名，"
                            "「旧版正在运行」这一条测不了")
        rc_open, msg_open = launch(old)
        if rc_open != 0:
            raise NoFixture("拉不起替身进程（LaunchServices 不可用？）: %s" % msg_open[-160:])
        seen = ""
        for _ in range(40):
            rc_ps, out_ps = run(["/bin/ps", "-Ao", "args"])
            seen = "\n".join(ln for ln in out_ps.splitlines() if ("sleep " + marker) in ln)
            if seen:
                break
            time.sleep(0.25)
        if not seen:
            raise NoFixture("替身没能起来，这条判据的前提没了")
        rc, msg, _ = run_from_volume(script, work, target_dir, lang=lang,
                                     SF_INSTALL_WAIT_SEC="3")
        trash = sorted(p.name for p in (route_home(work, "r2capps") / ".Trash").glob("*"))
        if rc == 0:
            return False, ("旧版明明在运行，脚本却报了成功（用户会以为新版已经装好）：\n"
                           + msg[-320:])
        if "正在运行" not in msg:
            return False, "停下来是对的，但没说清停在哪儿：输出里没有「正在运行」\n" + msg[-320:]
        if installed_version(target_dir / CURRENT_APP_NAME) != "0.0.1":
            return False, "停下来之前就已经动了旧版（原位那份版本变了）"
        if trash:
            return False, "还没开始装就把旧版移进了废纸篓：%s" % trash
        return True, "检测到在运行的旧版就停下、等它退出，且什么都没动（locale=%s）" % lang
    finally:
        # 卸卷 + **一定**杀掉替身：中途 raise（NoFixture / 判据红之外的异常）时
        # 不能留一个可启动的 .app 进程在机器上跑着 —— 那是给别的会话留坑。
        unmount(mnt)
        kill_by_path("sleep " + marker)


def check_corrupt_source_route(work: Path, fixture: str = "real") -> tuple[bool, str]:
    """R2d：卷里的包是坏的 → 只能得到一句"什么都没动"，不能得到坏包占着「应用程序」。

    实测过的现场（2026-10-13）：旧写法把 codesign 排在**移动旧版之后**，
    于是坏包安装失败时，安装位置躺着那份坏的、能用的旧版在废纸篓里 ——
    用户手上没有可用程序，而这正是他最需要一个还能用的程序的时机。
    """
    image, _ = build_user_dmg(work, "r2d", corrupt_payload=True, fixture=fixture)
    lang = user_lang()
    ok, why = locale_guard(lang or "")
    if not ok:
        return False, why
    mnt = work / "r2dmnt"
    try:
        mount(image, mnt)
        script, target_dir = volume_script(mnt, work, "r2dapps", old_version="0.0.1")
        rc, msg, _ = run_from_volume(script, work, target_dir, lang=lang,
                                     SF_INSTALL_ALLOW_RUNNING="1")
        trash = sorted(p.name for p in (route_home(work, "r2dapps") / ".Trash").glob("*"))
        if rc == 0:
            return False, "坏包居然装成功了（校验没排在前，或者根本没校验）"
        if installed_version(target_dir / CURRENT_APP_NAME) != "0.0.1":
            return False, ("坏包现场里旧版不在了 —— 原位版本读作 %r，"
                           "而废纸篓里有 %s" % (installed_version(target_dir / CURRENT_APP_NAME), trash))
        if trash:
            return False, "坏包这条应当什么都没动，废纸篓里却有 %s" % trash
        return True, "坏包在校验这一关就停下：旧版原位未动、废纸篓为空（locale=%s）" % lang
    finally:
        unmount(mnt)


def check_untaggable_scope_route(work: Path, script_text: str | None = None,
                                 fixture: str = "real") -> tuple[bool, str]:
    """R2e：包内某个文件的标签**清不掉**（uchg）时，脚本必须如实报失败并把旧版放回原位。

    为什么用 uchg：`xattr -dr` 对它是 EPERM（实测），于是"清"这一步真的会失败一半。
    而**只看包根的核验根本看不见内层残留**，会照样说一句「来历标签已清」——
    这正是变异 S9 种的那一处（核验从 `xattr -rl` 退回 `xattr -l`）必须被抓到的理由。
    实测两件事一起成立，这个现场才做得出来：quarantine 与 uchg 都能活着穿过
    ditto 与 HFS+/UDZO 卷，所以种在造卷之前就够了。
    """
    image, _ = build_user_dmg(work, "r2e", inner_tag="immutable",
                              script_text=script_text, fixture=fixture)
    lang = user_lang()
    ok, why = locale_guard(lang or "")
    if not ok:
        return False, why
    mnt = work / "r2emnt"
    try:
        mount(image, mnt)
        script, target_dir = volume_script(mnt, work, "r2eapps", old_version="0.0.1")
        rc, msg, _ = run_from_volume(script, work, target_dir, lang=lang,
                                     SF_INSTALL_ALLOW_RUNNING="1")
        if rc == 0:
            return False, ("清不掉的标签被当成清干净了（脚本报了成功，而包内仍带着标签）：\n"
                           + msg[-320:])
        if "标签" not in msg:
            return False, "停下来是对的，但那句话没说清是标签的问题：\n" + msg[-320:]
        if installed_version(target_dir / CURRENT_APP_NAME) != "0.0.1":
            return False, ("失败之后旧版没被放回原位（原位版本读作 %r）：\n"
                           % installed_version(target_dir / CURRENT_APP_NAME) + msg[-200:])
        return True, "清不掉的标签会如实报失败，并把旧版放回原位（locale=%s）" % lang
    finally:
        unmount(mnt)


def check_drag_and_release_route(work: Path) -> tuple[bool, str]:
    """R3（对照）：浏览器下 DMG + 手动拖进「应用程序」→ 确实带标签；FR-12 那条命令确实能清。"""
    image, _ = build_user_dmg(work, "r3")
    mnt = work / "r3mnt"
    try:
        mount(image, mnt)
        target_dir = work / "r3apps"
        target_dir.mkdir(parents=True, exist_ok=True)
        src = mnt / CURRENT_APP_NAME
        if not src.is_dir():
            return False, "卷里找不到 %s" % CURRENT_APP_NAME
        rc, msg = run(["/bin/cp", "-R", str(src), str(target_dir)])
        if rc != 0:
            return False, "cp 失败: " + msg[-200:]
        installed = target_dir / CURRENT_APP_NAME
        if not has_quarantine(installed):
            return False, ("缺陷现场复现不了：从带标签的 DMG 里拖出来的 app 竟然不带标签 —— "
                           "说明那道墙变了、或贴标签的手法失效，这条对照不可信")
        rc, msg = run([XATTR, "-dr", QUARANTINE, str(installed)])
        if rc != 0 or has_quarantine(installed):
            return False, "现行放行命令没能清掉标签: " + msg[-200:]
        return True, "拖拽确实带上标签（缺陷现场复现）；FR-12 那条命令确实清得掉"
    finally:
        unmount(mnt)


def run_helper(helper: Path, work: Path, top_name: str, bundle_id: str = BUNDLE_ID,
               version: str = APP_VERSION) -> tuple[str, str]:
    """真跑一次更新助手（目标目录在临时区），返回 result.json 里的 status/message。"""
    d = work / ("h_" + top_name[:-4] + "_" + uuid.uuid4().hex[:6])
    d.mkdir(parents=True, exist_ok=True)
    make_fake_app(d, top_name, version=version, bundle_id=bundle_id)
    archive = zip_from(d / top_name, d / "ota.zip")
    old_dir = d / "Applications"
    old_dir.mkdir(exist_ok=True)
    make_fake_app(old_dir, CURRENT_APP_NAME, version="1.0.0", bundle_id=bundle_id)
    result = d / "result.json"
    rc, msg = run(["/bin/sh", str(helper),
                   "--pid", "2147483646", "--archive", str(archive),
                   "--sha256", sha256_of(archive), "--version", version,
                   "--target", str(old_dir / CURRENT_APP_NAME),
                   "--result", str(result)])
    payload = {}
    if result.is_file():
        try:
            payload = json.loads(result.read_text(encoding="utf-8"))
        except ValueError:
            payload = {"status": "unparsable", "message": result.read_text(encoding="utf-8")[:120]}
    if not payload:
        payload = {"status": "no_result", "message": (msg or "")[-160:], "rc": rc}
    return str(payload.get("status", "?")), str(payload.get("message", "?"))


def check_helper_accepts_both_names(work: Path, helper: Path | None = None) -> tuple[bool, str]:
    """R4：助手必须认得改名前后的两种包内顶层名，而且真的能把包**解到底**。

    判据从 v1.4.59 起收紧了一档，因为原来的写法放过了一次真实缺陷：旧判据只要求
    "别停在 archive_layout_invalid"，于是助手在**更早**的解包预检就死掉时（本轮 S6 把
    ZIP 预检从 /usr/bin/python3 换成 zipinfo+awk，中文条目名让 macOS awk 直接中止）
    R4 仍然是绿的。替身 app 一律不签名，所以正当输入的终点必然是
    new_app_signature_invalid —— 停在它**之前**的任何一站都表示"这个助手装不了任何包"，
    而停在它之后（rolled_back / installed）说明夹具的签名属性被改坏了，同样要红。
    """
    helper = helper or HELPER
    past_unpacking = "new_app_signature_invalid"
    bad = []
    for name in [CURRENT_APP_NAME] + LEGACY_APP_NAMES:
        status, message = run_helper(helper, work, name)
        if message != past_unpacking:
            bad.append("%s → %s/%s（应当停在 %s；停在它之前=解不了包，之后=夹具被改坏）"
                       % (name, status, message, past_unpacking))
    # 反向：顶层名对不上的包必须被拒（否则这条判据是道永远过的关）
    status, message = run_helper(helper, work, "WrongName.app")
    if message != "archive_layout_invalid":
        bad.append("反向种错没被抓到：WrongName.app → %s/%s" % (status, message))
    if bad:
        return False, "；".join(bad)
    return True, "两代顶层名都能一路解到签名校验，第三种名字仍被拒（archive_layout_invalid）"



# 路线表：**唯一一份**名单，main() 与 tests/test_gate_clean_machine.py 都从这里取。
# 为什么必须是数据而不是散在 main 里的 lambda：那条"一条都没跑成就不许给结论"的判据
# 原先自己写死了五个函数名，本轮加了 R2b/R2c/R2d/R2e 之后它漏掉四条 —— 于是"没有输入
# 不等于全过"这道红线自己变成了假门（写死清单回答不了"还有第几处没进清单"，同 §3 那八处改名）。
# 表里存的是**函数名**而不是函数对象，这不是风格：`tests/test_gate_clean_machine.py`
# 靠 patch 模块属性来模拟"这条路线在这台机器上没夹具"，存对象的话 main() 拿着旧引用
# 照跑不误 —— 那条"一条都没跑成就不许给结论"的红线当时当场变成假门（实测红在
# 「R0 ✓ 通过」而其余八条报 TypeError）。按名字现取，patch 才打得到。
ROUTES: tuple[tuple[str, str], ...] = (
    ("R0 名字同源（两代顶层名都钉住）", "check_name_source"),
    ("R1 curl 一行（零放行）", "check_curl_route"),
    ("R2 DMG 内双击安装（递归清标签）", "check_dmg_launcher_route"),
    ("R2b 老用户升级（旧版在位 → 废纸篓）", "check_upgrade_route"),
    ("R2c 旧版正在运行时必须停下等它退出", "check_running_instance_route"),
    ("R2d 坏包只报「什么都没动」", "check_corrupt_source_route"),
    ("R2e 清不掉的标签不许报成功", "check_untaggable_scope_route"),
    ("R3 拖拽 + 放行命令（对照）", "check_drag_and_release_route"),
    ("R4 更新助手认两代包名", "check_helper_accepts_both_names"),
)


def route_names() -> list[str]:
    """给判据用的函数名清单（打桩、数条数都从这里取，不再各写一份）。"""
    return [name for _label, name in ROUTES]


def route_callable(name: str):
    fn = globals().get(name)
    if not callable(fn):
        raise RuntimeError("路线表里的 %s 在本模块找不到 —— 名单与实现漂移了" % name)
    return fn


# ---------------------------------------------------------------- 变异自检

def _red(pair: tuple[bool, str], note: str) -> tuple[bool, str]:
    """把一条路线的 (通过, 说明) 翻译成变异判据：**路线报红 = 种错被抓到**。"""
    ok, detail = pair
    return (not ok, detail or note)


def mutate_case(label: str, fn) -> tuple[str, bool | None, str]:
    """跑一条变异，返回 (标签, 抓到没有, 说明)。第三种状态 None = **这条没跑起来**。

    为什么必须分三态而不是"要么过要么红"：S2 那一条的前提是"这台机器上有装好的
    真产物可复制"（real_app 找不到就抛 NoFixture）。云端那台机器是全新的，
    /Applications 与 dist/ 都是空的 —— 旧实现让异常直接穿出 main()，于是这道闸门
    **崩**在干净机器上，被链上用例读成"闸门自证失败"，看起来像产物坏了，
    实际是夹具不在（2026-10-10 云端两条红之一就是这一枚）。
    但"没跑"绝不能记成"通过"：它单独计数、大声打印，且一条都没跑起来时拒绝给结论。

    名字不带下划线是故意的：三态判据本身要有用例直接喂两种异常进去对拍，
    走 main() 的话每条用例都得重跑一遍 13 秒的真夹具链（ditto/hdiutil/codesign），
    代价换不到额外的可信度。
    """
    try:
        got_it, note = fn()
        return (label, bool(got_it), note)
    except NoFixture as prem:
        return (label, None, "前提缺失，这条没跑：%s" % prem)
    except Exception as exc:  # noqa: BLE001  非前提类的意外一律记红，不给静默跳过的路
        return (label, False, "核验没跑起来：%r" % (exc,))


def self_test(work: Path) -> list[tuple[str, bool | None, str]]:
    """种若干处错（数量随判据走，运行时打印实际条数），每一处都必须让对应那条路线真的报红。

    种错之间互相独立（各自一个临时子目录），所以**并发跑**：这一轮的判据从 7 处涨到 15 处，
    串行实测 48 秒，而 `tests/test_release_gates.py::test_every_chain_tool_can_prove_it_fails`
    跑 `--self-test` 的那个用例受全关卡 60 秒单用例上限约束（第一次撞上时被 0:01:00 掐掉，
    报的是 Timeout 不是缺陷）。并发不是为了快，是为了让"加判据"不必以"删判据"为代价。
    顺序仍按登记顺序返回，打印出来的一览表与串行版一致。
    """
    launcher_body = launcher_script().read_text(encoding="utf-8")
    text = HELPER.read_text(encoding="utf-8")
    work.mkdir(parents=True, exist_ok=True)

    cases: list[tuple[str, Callable[[], tuple[str, bool | None, str]]]] = []

    # S1：助手退回只认旧名（v1.4.55 改名时真实发生过的漏法）→ R4 必须红
    patched = work / "helper_legacy_only.sh"
    patched.write_text(text.replace('for _name in "%s.app" ' % APP_DISPLAY_NAME,
                                    "for _name in "), encoding="utf-8")
    cases.append(("S1 助手退回只认旧名",
                  lambda: mutate_case("S1 助手退回只认旧名",
                                      lambda: _red(check_helper_accepts_both_names(
                                          work / "s1", helper=patched), "居然还是绿的"))))

    # S2：卷内脚本本身被贴标签（等于双击会被拦）→ R2 必须红
    cases.append(("S2 双击脚本带着标签",
                  lambda: mutate_case("S2 双击脚本带着标签",
                                      lambda: _red(check_dmg_launcher_route(
                                          work / "s2", stamp_script=True, fixture="light"),
                                          "居然还是绿的（判据看不见标签）"))))

    # S3 / S3b：标签分别贴在下载物与装好的 app 上 → R1 两条断言各必须红
    cases.append(("S3 curl 取回的包带着标签",
                  lambda: mutate_case("S3 curl 取回的包带着标签",
                                      lambda: _red(check_curl_route(work / "s3",
                                                                    stamp_download=True),
                                                   "居然还是绿的"))))
    cases.append(("S3b 装好的 app 带着标签",
                  lambda: mutate_case("S3b 装好的 app 带着标签",
                                      lambda: _red(check_curl_route(work / "s3b",
                                                                    stamp_installed=True),
                                                   "居然还是绿的"))))

    # S4：助手里"当前产品名"的字面量被删掉 → R0 必须红
    stripped = text.replace('"%s.app"' % APP_DISPLAY_NAME, '"Deleted.app"', 1)
    cases.append(("S4 助手不再钉当前产品名",
                  lambda: mutate_case("S4 助手不再钉当前产品名",
                                      lambda: _red(check_name_source(helper_text=stripped),
                                                   "名字漂移没被拦住"))))

    # S5：夹具退化检查 —— zip 少带 --keepParent 时，R4 必须以 layout_invalid 红
    def _s5():
        no_parent = work / "s5.zip"
        src = make_fake_app(work / "s5", CURRENT_APP_NAME)
        run(["/usr/bin/ditto", "-c", "-k", str(src), str(no_parent)])
        status, message = _run_helper_on_zip(HELPER, work / "s5run", no_parent)
        return message == "archive_layout_invalid", "got %s/%s" % (status, message)

    cases.append(("S5 夹具少了顶层目录时会被识破", lambda: mutate_case(
        "S5 夹具少了顶层目录时会被识破", _s5)))

    # S6：把 ZIP 预检的 LC_ALL=C 摘掉 → R4 必须红。
    #     这是本轮真实踩到的缺陷：ditto 打出的中文条目名经 zipinfo 按代码页转换后是
    #     非法 UTF-8，macOS 的 awk 一碰到就整条中止，助手在任何**一个包**上都解不动。
    #     R4 的旧判据（只盯 archive_layout_invalid）当时是绿的 —— 所以这条变异同时
    #     在验"判据收紧之后真的能抓到"，而不只是在验被测对象。
    stripped_text = text.replace("LC_ALL=C ", "")
    cases.append(("S6 预检少了 LC_ALL=C（种错本身要成立）",
                  lambda: ("S6 预检少了 LC_ALL=C（种错本身要成立）",
                           stripped_text != text and "LC_ALL=C" not in stripped_text,
                           "没能把 LC_ALL=C 摘干净，这条变异是空跑")))
    no_locale = work / "helper_no_lc_all.sh"
    no_locale.write_text(stripped_text, encoding="utf-8")
    cases.append(("S6 中文条目名让 awk 中止时 R4 会红",
                  lambda: mutate_case("S6 中文条目名让 awk 中止时 R4 会红",
                                      lambda: _red(check_helper_accepts_both_names(
                                          work / "s6", helper=no_locale),
                                          "R4 竟然还是绿的（判据又变成了只看包名）"))))

    # S7：发版脚本忘了贴图标（夹具里直接不贴）→ R2 必须红。
    cases.append(("S7 卷内脚本没贴图标",
                  lambda: mutate_case("S7 卷内脚本没贴图标",
                                      lambda: _red(check_dmg_launcher_route(
                                          work / "s7", with_icon=False, fixture="light"),
                                          "居然还是绿的（判据看不见图标）"))))
    # S8：贴了图标、忘了隐藏 `.command` 后缀 → R2 必须红。
    #     S7 与 S8 必须分开种：两步写的是**同一条元数据的同一格 flags**。
    cases.append(("S8 卷内脚本仍露着 .command 后缀",
                  lambda: mutate_case("S8 卷内脚本仍露着 .command 后缀",
                                      lambda: _red(check_dmg_launcher_route(
                                          work / "s8", hide_ext=False, fixture="light"),
                                          "居然还是绿的（判据看不见显示名）"))))

    # ---- 下面是 2026-10-13 对着「一键安装」那四条实测缺陷补的三处种错 ----
    # S9：清除动作退回不递归（`xattr -d`）→ 包内那枚标签活下来 → R2 必须红。
    no_recursive_clear = launcher_body.replace(
        "/usr/bin/xattr -dr com.apple.quarantine", "/usr/bin/xattr -d com.apple.quarantine")
    cases.append(("S9 种错本身成立（清除被改成不递归）",
                  lambda: ("S9 种错本身成立（清除被改成不递归）",
                           no_recursive_clear != launcher_body
                           and "xattr -dr com.apple.quarantine" not in no_recursive_clear,
                           "没能改掉清除那一步，这条变异是空跑")))
    cases.append(("S9 清标签不递归时 R2 会红",
                  lambda: mutate_case("S9 清标签不递归时 R2 会红",
                                      lambda: _red(check_dmg_launcher_route(
                                          work / "s9", script_text=no_recursive_clear,
                                          fixture="light"),
                                          "居然还是绿的（内层残留看不见）"))))

    # S9b：核验退回只看包根（`xattr -l`），配上**清不掉**的那枚内层标签（uchg）→ R2e 必须红。
    blind_verify = launcher_body.replace('/usr/bin/xattr -rl "$STAGE"',
                                         '/usr/bin/xattr -l "$STAGE"')
    cases.append(("S9b 种错本身成立（核验被改成只看包根）",
                  lambda: ("S9b 种错本身成立（核验被改成只看包根）",
                           blind_verify != launcher_body
                           and 'xattr -rl "$STAGE"' not in blind_verify,
                           "没能改掉核验那一步，这条变异是空跑")))
    cases.append(("S9b 标签清不掉而核验只看包根时 R2e 会红",
                  lambda: mutate_case("S9b 标签清不掉而核验只看包根时 R2e 会红",
                                      lambda: _red(check_untaggable_scope_route(
                                          work / "s9b", script_text=blind_verify,
                                          fixture="light"),
                                          "居然还是绿的（它把清不掉的标签报成了成功）"))))

    # S10：关卡自己退回"开发机那一侧"的 C locale → 这一族判据当场失效，必须红。
    cases.append(("S10 核验跑在 C locale 时 R2 会红",
                  lambda: mutate_case("S10 核验跑在 C locale 时 R2 会红",
                                      lambda: _red(check_dmg_launcher_route(
                                          work / "s10", force_lang="C", fixture="light",
                                          check_appearance=False),
                                          "居然还是绿的（站在安全一侧测这一族缺陷等于没测）"))))

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(fn) for _label, fn in cases]
        caught = [f.result() for f in futures]
    return caught


def _run_helper_on_zip(helper: Path, work: Path, archive: Path) -> tuple[str, str]:
    work.mkdir(parents=True, exist_ok=True)
    old_dir = work / "Applications"
    old_dir.mkdir(exist_ok=True)
    make_fake_app(old_dir, CURRENT_APP_NAME, version="1.0.0")
    result = work / "result.json"
    rc, msg = run(["/bin/sh", str(helper), "--pid", "2147483646", "--archive", str(archive),
                   "--sha256", sha256_of(archive), "--version", APP_VERSION,
                   "--target", str(old_dir / CURRENT_APP_NAME), "--result", str(result)])
    try:
        payload = json.loads(result.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "no_result", (msg or "")[-160:]
    return str(payload.get("status", "?")), str(payload.get("message", "?"))


# ---------------------------------------------------------------- 入口

def main() -> int:
    # 未知参数退 2，**绝不落到默认动作里**（AGENTS §3.1 v1.4.61 那条链上规矩）：
    # 默认动作是"跑五条真核验"，一个打错的参数就把它跑起来，等于把拼错当成授权。
    unknown = [a for a in sys.argv[1:] if a != "--self-test"]
    if unknown:
        print("!! 不认识这些参数：%s\n   用法：verify_install_paths.py [--self-test]"
              % "、".join(unknown))
        return 2
    work = Path(tempfile.mkdtemp(prefix="sf_install_paths_"))
    results: list[tuple[str, str, str]] = []
    try:
        if "--self-test" in sys.argv:
            cases = self_test(work / "st")
            # 数量从结果里算，不写死在文案里：写死的那次是"种七处错"，加到第九条之后
            # 文案就成了假话 —— 而这句话正是这条闸门对自己可信度的描述。
            print("== 变异自检：种 %d 处错，每一处都必须被抓到 ==" % len(cases))
            # 变量名带 _cases 后缀：下面"跑五条路线"那段用同名变量装字符串列表，
            # 复用会让类型注解自相矛盾（mypy 当场报 misc/arg-type 各一条）。
            ran_cases = [c for c in cases if c[1] is not None]
            missed_cases = [c for c in cases if c[1] is False]
            absent_cases = [c for c in cases if c[1] is None]
            for label, got_it, note in cases:
                mark = "✓" if got_it else ("✗" if got_it is False else "⊘")
                tail = "" if got_it else "  ← %s%s" % ("假门：" if got_it is False else "",
                                                       note)
                print("  %s %s%s" % (mark, label, tail))
            if missed_cases:
                print("!! 核验工具自己不可信：至少一条判据抓不到种错")
                return 1
            if not ran_cases:
                # 零输入 = 没有结论。这条红线是给"关卡范围失效"准备的（同
                # test_discipline_scan 的 min_files）：否则七条全被"没夹具"吞掉时，
                # 干净机器照样打印"自检通过"。
                print("!! 一条变异都没跑起来：没有输入不等于全过，拒绝给结论")
                return 1
            if absent_cases:
                print("!! WARNING 有 %d 条没跑（缺夹具），没测到的不算通过：%s"
                      % (len(absent_cases),
                         "、".join(label for label, _, _ in absent_cases)))
            print("== 变异自检通过：%d 项抓到，0 项失败，%d 项没跑 =="
                  % (len(ran_cases), len(absent_cases)))
            return 0

        for label, name in ROUTES:
            fn = route_callable(name)
            try:
                ok, detail = fn(work)
                state = "pass" if ok else "fail"
            except NoFixture as skip:
                state, detail = "skip", str(skip)
            except Exception as exc:  # noqa: BLE001  前提坏掉也要如实报红，不能静默跳过
                state, detail = "fail", "核验没跑起来：%r" % (exc,)
            results.append((label, state, detail))
            print("%s %s\n     %s" % ({"pass": "✓", "fail": "✗", "skip": "⊘"}[state],
                                      label, detail))
    finally:
        # R2e 在包内种的是 uchg（就是"清不掉"这件事本身），带着它 rmtree 删不掉；
        # 收尾必须先递归卸掉，不然每次跑完都在系统临时目录里留一份删不掉的产物。
        clear_immutable(work)
        shutil.rmtree(work, ignore_errors=True)

    failed = [label for label, state, _ in results if state == "fail"]
    skipped = [label for label, state, _ in results if state == "skip"]
    passed = [label for label, state, _ in results if state == "pass"]
    print("")
    if failed:
        print("!! 红 %d 条：%s" % (len(failed), "、".join(failed)))
        print("   （版本 v%s，名字唯一来源 sf_constants.APP_DISPLAY_NAME=%s）"
              % (APP_VERSION, APP_DISPLAY_NAME))
        return 1
    if not passed:
        # 2026-10-12 全方位复核 F5：自检档早有这条红线（上面 `not ran_cases` 那一段），
        # 默认档却没有 —— 于是"这台机器什么都没有"能换来一句"核验通过"。
        # 一条都没跑成就是没有输入，与"跑过且都过"是两件不同的事，必须分开说。
        print("!! %d 条路线一条都没跑成（全被记成没夹具）：没有输入不等于全过，拒绝给结论"
              % len(results))
        for label, _, detail in results:
            print("   ⊘ %s —— %s" % (label, detail))
        return 1
    tail = "（跳过 %d 条：%s —— 没测到的不算通过）" % (len(skipped), "、".join(skipped)) \
        if skipped else ""
    print("== 首装与更新路线核验通过：真跑 %d 条、装完不带来历标签、助手认两代包名 %s=="
          % (len(passed), tail))
    return 0


if __name__ == "__main__":
    sys.exit(main())
