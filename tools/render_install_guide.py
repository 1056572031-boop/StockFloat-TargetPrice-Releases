#!/usr/bin/env python3
"""渲染对外安装说明（一行 curl 安装 + DMG 双击安装 + 兜底放行）。

为什么要有这个渲染器，而不是手写一份 markdown：
  那行命令里带着**版本号与资产名**。手写的下一轮就旧了，而旧指引的表现形式是
  「用户照做、404、以为你的程序有问题」—— 这类错不会有任何地方报错。
  版本唯一来源是 sf_constants.APP_VERSION，资产名与仓库/标签唯一来源是
  sf_constants.py，本脚本通过轻量身份读取器共享发行配置，不复制常量。

用法
    ./.venv/bin/python tools/render_install_guide.py            # 写到 docs/INSTALL.md
    ./.venv/bin/python tools/render_install_guide.py --print    # 只打印
    ./.venv/bin/python tools/render_install_guide.py --check    # 核验现存文件是不是本轮版本（发版闸门）
    ./.venv/bin/python tools/render_install_guide.py --self-test  # 变异自检（发版链要求，见下）
    默认使用获准保留的公开支持邮箱，允许 SF_CONTACT_EMAIL 显式覆盖。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sf_constants import (  # noqa: E402  （安装命令与产品两代名称必须同源）
    APP_DISPLAY_NAME,
    APP_VERSION,
    DESKTOP_OUTPUT_DIR,
    DOWNLOAD_REDIRECT_BASE,
    LEGACY_APP_DISPLAY_NAMES,
)

from release_identity import SUPPORT_EMAIL  # noqa: E402
import make_update_assets  # noqa: E402  （资产命名；发行身份来自 sf_constants）
from verify_install_paths import launcher_script  # noqa: E402  （卷内双击脚本：名字按 installer/*.command 发现）
import apply_installer_icon as AIC  # noqa: E402  （卷内条目在 Finder 里的**显示名**，与发版链同一份实现）


def probe_asset(url: str) -> tuple[str, str]:
    """Follow the public redirect and require the named release asset to exist.

    The installation guide names a specific user download.  A 302 from the
    analytics entry only proves that its route parser accepted the query; it
    does not prove that GitHub has published the requested byte stream.
    """
    try:
        p = subprocess.run([
            "/usr/bin/curl", "-fsSIL", "--max-time", "25", "--max-redirs", "4",
            "-o", "/dev/null", "-w", "%{http_code}\n%{url_effective}", url,
        ], capture_output=True, text=True)
    except OSError as exc:
        return "unknown", "探不了（%r），这条未核验" % (exc,)
    lines = (p.stdout or "").splitlines()
    code = lines[0].strip() if lines else ""
    final_url = lines[1].strip() if len(lines) > 1 else ""
    expected = archive_name()
    parsed = urllib.parse.urlsplit(final_url)
    if p.returncode == 0 and code == "200" and parsed.hostname in {
            "github.com", "objects.githubusercontent.com", "github-releases.githubusercontent.com",
            "release-assets.githubusercontent.com"} and parsed.path.endswith("/" + expected):
        return "ok", "最终 HTTP 200：" + final_url
    if code == "404" or (p.returncode and code == "404"):
        return "missing", "HTTP 404：这一轮的包还没推上线"
    if p.returncode != 0 or not code or not code.isdigit():
        return "unknown", "拿不到最终状态码（网络、代理或重定向异常），这条未核验"
    return "missing", "最终下载地址不是本轮 GitHub 资产（HTTP %s：%s）" % (code, final_url or "无")

# 放 docs/ 而不是顶层：build_public_snapshot.py 把 .sf-opensource/docs/*.md 整批带进公开快照，
# 顶层文件要逐个登记；这份说明是要对外的，跟着 docs 走才不会哪天被漏掉。
OUT_DEFAULT = ROOT / "docs" / "INSTALL.md"
CONTACT_ENV = "SF_CONTACT_EMAIL"


def archive_name(version: str = APP_VERSION) -> str:
    return "Skyrocket-%s-macos.zip" % version


def checksums_name(version: str = APP_VERSION) -> str:
    return "Skyrocket-%s-SHA256SUMS.txt" % version


def download_url(version: str = APP_VERSION) -> str:
    # 公开下载统一走匿名计数入口：它只记录版本、文件、通路类别与时间，然后 302 到
    # GitHub 官方资产。安装包字节仍由 GitHub 提供；入口故障时统计失败不会挡住下载。
    query = urllib.parse.urlencode({"version": version, "asset": archive_name(version)})
    return "%s/download?%s" % (DOWNLOAD_REDIRECT_BASE.rstrip("/"), query)


def manifest_sha256() -> str | None:
    """Read current asset digest availability, without embedding it back into the app."""
    for cand in (ROOT / "build" / "update_assets" / ("V%s" % APP_VERSION) / "Skyrocket-update.json",
                 Path(DESKTOP_OUTPUT_DIR) / "Skyrocket-update.json"):
        if not cand.is_file():
            continue
        try:
            data = json.loads(cand.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if data.get("version") != APP_VERSION:
            continue
        digest = (data.get("archive") or {}).get("sha256")
        if isinstance(digest, str) and len(digest) == 64:
            return digest
    return None


def curl_line() -> str:
    """首次安装专用：任一两代 app 存在时拒绝解压合并。"""
    name = archive_name()
    app_paths = ["/Applications/%s.app" % APP_DISPLAY_NAME]
    app_paths.extend("/Applications/%s.app" % legacy for legacy in LEGACY_APP_DISPLAY_NAMES)
    absent = " && ".join("test ! -e '%s'" % path for path in app_paths)
    return ("%s || { echo '已安装 Skyrocket：请改用 DMG 里的「一键安装」升级。' >&2; exit 1; }; "
            "curl -fsSL -o ~/Downloads/%s '%s'"
            " && ditto -x -k ~/Downloads/%s /Applications/"
            " && open '/Applications/%s.app'") % (
                absent, name, download_url(), name, APP_DISPLAY_NAME)


def release_line() -> str:
    return "xattr -dr com.apple.quarantine /Applications/%s.app" % APP_DISPLAY_NAME


def contact() -> str:
    """Keep the approved public mailbox unless explicitly overridden."""
    return os.environ.get(CONTACT_ENV) or SUPPORT_EMAIL


# FR-12 的四条硬约束里，有两条能在**代码块**上机器核验（正文里的否定句不算违规：
# "它不删除任何文件"这种描述必须允许存在，否则闸门会因为诚实的措辞而红）。
FORBIDDEN_IN_COMMANDS = ("sudo", "rm ", " -rf", "chown", "curl -s | sh", "| sh")


def lint_commands(text: str) -> list[str]:
    """扫围栏代码块里的每一条命令：不许出现 sudo/删除/改属主，清标签只许动自己那个 bundle。"""
    problems = []
    in_block = False
    for raw in text.splitlines():
        if raw.strip().startswith("```"):
            in_block = not in_block
            continue
        if not in_block or not raw.strip() or raw.strip().startswith("#"):
            continue
        for bad in FORBIDDEN_IN_COMMANDS:
            if bad in raw:
                problems.append("命令里出现了「%s」：%s" % (bad, raw[:70]))
        if "xattr" in raw and "/Applications/%s.app" % APP_DISPLAY_NAME not in raw:
            problems.append("清标签的命令没有把路径写死到本程序自己的 bundle：%s" % raw[:70])
    return problems


def lint_placeholders(text: str, allow_unset_contact: bool = False) -> list[str]:
    """对外产物里不许留没替换的占位符（v1.4.60，复核 F3）。

    两条判据都是"发出去就没人能用的死路"：
      · 字面 `%s` —— 少写一步 % 运算就会漏进文档，用户照着找不到文件；
      · 联系渠道占位 —— SF_CONTACT_EMAIL 没设时正文写着"发给（发版前把这里换成专用
        邮箱：SF_CONTACT_EMAIL）"，卡住的人照这句话一个人都联系不上。
    默认判红。确需先出稿时 `--allow-unset-contact` 只放行第二条，并大声打 WARNING
    （与 build_release.sh 的 --skip-tests 同一口径：可以绕，但绕了必须看得见）。
    """
    problems = []
    for i, raw in enumerate(text.splitlines(), 1):
        if "%s" in raw:
            problems.append("第 %d 行留着没替换的占位符 %%s：%s" % (i, raw[:70]))
    if CONTACT_ENV in text and not allow_unset_contact:
        problems.append("联系渠道还没设：文中留着 %s 占位 —— "
                        "export %s=专用邮箱 后重渲染，或明确加 --allow-unset-contact"
                        % (CONTACT_ENV, CONTACT_ENV))
    return problems


def render(local_only: bool = False) -> str:
    if local_only:
        return "\n".join([
            "# Skyrocket v%s 本机安装说明" % APP_VERSION,
            "",
            "本构建仅用于本机覆盖安装，未发布 GitHub/OTA，不提供不存在的线上下载命令。",
            "",
            "## 安装与核验",
            "",
            "1. 使用本机生成的 `Skyrocket-V%s.dmg`，双击磁盘里的「一键安装」。" % APP_VERSION,
            "2. 应用安装到 `/Applications/Skyrocket.app`，原有股票、目标价与窗口设置保留。",
            "3. 核对本地交付记录中的 SHA-256；来源不明或校验失败时停止安装。",
            "",
            "```sh",
            "shasum -a 256 ~/Desktop/Skyrocket/Skyrocket-V%s.dmg" % APP_VERSION,
            "```",
            "",
            "## 签名与隐私边界",
            "",
            "未签名、未公证：本项目没有 Apple Developer ID 开发者签名，也没有 Apple 公证。",
            "固定项目自签名用于版本身份核验；历史包使用 ad-hoc，均不表示 Apple 已审核。",
            "不会关闭全系统 Gatekeeper，不清空个人配置，不修改用户钥匙串 ACL。",
            "行情查询仍会向第三方发送股票代码或搜索词；更新和使用边界见随包 README。",
            "需要排障时主动导出诊断包，发送前检查个人信息；诊断包不会自动上传。",
            "漏洞按 SECURITY.md 私下报告，完整许可见 LICENSE 与 NOTICE.md。",
            "",
        ])
    ver = APP_VERSION
    lines = [
        "# %s 安装说明（v%s）" % (APP_DISPLAY_NAME, ver),
        "",
        "提供 DMG 和 ZIP 两种安装包，普通用户建议用下文的 DMG 双击安装。",
        "系统要求：Apple 芯片的 Mac，**仅支持 macOS 26 或更高版本**；"
        "不提供 macOS 25 及更早版本、Intel、Windows 或 Linux 安装包。"
        "装机版实机核验来自 macOS 27，macOS 26 由云端 CI 完成全量回归与原生平台冒烟、尚未做装机实机验收；"
        "先在「苹果菜单 → 关于本机」确认版本，低于支持范围时请勿安装。",
        "**未签名、未公证**：本项目没有 Apple Developer ID 开发者签名和 Apple 公证。",
        "本地签名用于完整性与版本身份检查：历史包使用临时 ad-hoc，后续固定签名包使用项目自签名证书；"
        "两者均不代表 Apple 已确认开发者身份或审核程序。",
        "请只从[官方发行页](https://github.com/%s/releases/tag/%s)下载，"
        "核对本版文件名和 SHA-256；校验不符或来源不明时停止安装，不要盲目放行。"
        % (make_update_assets.REPO, urllib.parse.quote(make_update_assets.TAG, safe="")),
        "不会关闭全系统安全保护；以下放行方式仅在你确认信任本项目后使用。",
        "发行页的 `%s` 列出本版 DMG、应用 ZIP 和源码 ZIP 校验值。" % checksums_name(),
        "在终端执行下列命令，并将输出与校验清单的同名文件一行对照：",
        "",
        "```sh",
        "shasum -a 256 ~/Downloads/Skyrocket-V%s.dmg" % ver,
        "```",
        "装好后可自动更新；旧名客户端和 v1.4.58 及更早版本须先手动装一次当前版本。",
        "",
        "## 一、ZIP 安装（仅首次安装；熟悉终端时使用）",
        "",
        "打开「终端」（访达 → 应用程序 → 实用工具，或启动台里搜 terminal），",
        "把下面这一整行原样粘贴进去，按回车：",
        "",
        "```sh",
        curl_line(),
        "```",
        "",
        "这一行会先确认「应用程序」里没有旧版，再经官方下载入口跳转到 github.com 取回本版安装包、解压并打开它。",
        "它不使用 `sudo`，不删除任何文件，也不会碰你别的目录。",
        "已经装过旧版本时，它会停下并提示你使用下方 DMG 里的「一键安装」；不要用 ZIP 直接覆盖旧图标。",
        "",
        "为什么这样装不会被系统拦：拦下未签名程序靠的是文件上那张「来历标签」，",
        "而那张标签是**浏览器或微信下载**这个动作贴上去的，用上面这行命令取回来的文件不带它，",
        "这不会增加文件的可信度；企业设备策略仍可能阻止运行。",
        "",
        "**已经装过旧版本的人请用第二种**（它会先把旧版移到废纸篓再装新版）。",
        "",
        "## 二、双击安装（拿到的是 DMG，且不想碰终端）",
        "",
        # v1.4.60（复核 F3）：这一行原先少了一步 % 运算，发出去的文档里字面写着
        # `Skyrocket-V%s.dmg`，用户对着桌面 Skyrocket 文件夹里的真实包找不着北。
        "1. 打开桌面上的 `Skyrocket` 文件夹，双击其中的 `Skyrocket-V%s.dmg`，它会挂载成一个白色磁盘图标；" % ver,
        # 这一步写的是用户**看得见**的那个名字，不是磁盘上的真名：发版链第 4 步会把
        # `.command` 从显示里去掉（磁盘上的名字没变，双击仍然交给终端执行）。
        # 为什么走 AIC.volume_displayed_name 而不是在这里自己切一刀：切法与"卷里到底
        # 显不显示后缀"是同一件事的两侧，只能有一份实现 —— 发版链哪天不隐藏了，
        # 这句得跟着变，而各说各话不会有任何地方报错。
        "2. 打开那个磁盘，**双击 `%s`**（图标是一枚银灰色驱动器）；"
        % AIC.volume_displayed_name(launcher_script().name),
        "3. 它会把当前名称和旧名称的版本移到废纸篓（不是删除）、装入新版、清掉刚装入那一份的来历标签、",
        "   校验包体完整性，然后尝试自动打开；没有自动打开时，到「应用程序」中双击 Skyrocket。",
        "",
        "如果你只看到「无法打开，因为来自身份不明的开发者」，那是 macOS 在问你确不确认：",
        "确认来源和校验值无误后，到「系统设置 → 隐私与安全性」查看是否有「仍要打开」。",
        "不同系统或企业设备可能没有这个入口；不要为此关闭全系统 Gatekeeper。",
        "",
        "## 三、兜底：照旧拖进「应用程序」，再跑一条放行命令",
        "",
        "1. 打开 DMG，把 %s.app 拖到旁边的「Applications」替身上；" % APP_DISPLAY_NAME,
        "2. 第一次打开被拦时，在终端里粘贴这一行并按回车：",
        "",
        "```sh",
        release_line(),
        "```",
        "",
        "它只清除这一个程序路径上的来历标签，不清系统里的其他任何东西。",
        "",
    ]
    # The ZIP includes this guide. Embedding its own digest would change the
    # build inputs after packaging and create a circular archive dependency.
    lines += [
        "## 核对 ZIP 安装包",
        "",
        "```sh",
        "shasum -a 256 ~/Downloads/%s" % archive_name(),
        "```",
        "",
        "将输出与官方发行页 `%s` 中 `%s` 的同名条目对照；不一致时停止安装。"
        % (checksums_name(), archive_name()),
        "",
    ]
    lines += [
        "## 照做之后还是装不上",
        "",
        "不要反复试。请把以下三样发给 %s：" % contact(),
        "",
        "- 你用的是第几种装法、卡在哪一步（截图最好）；",
        "- 你的 macOS 版本（苹果菜单 → 关于本机）；",
        "- 如果程序曾经打开过：右键悬浮窗 →「导出诊断包」，把直接生成在桌面上的 zip 一并发来。",
        "",
        "诊断包不会自动上传，但可能有股票代码、设备信息、运行状态和脱敏后的错误日志。",
        "发送前请检查并删除不愿公开的信息，不要把完整诊断包直接贴进公开 Issue。",
        "配置和目标价留在本机；行情查询会向第三方发送股票代码或搜索词，更新会访问 GitHub。",
        "完整隐私说明与行情接口使用边界见随包 README；漏洞请按 SECURITY.md 私下报告。",
    ]
    return "\n".join(lines) + "\n"


def self_test() -> int:
    """变异自检：三种"发出去就是死路"的形状都必须被抓到，干净样本必须放行。

    为什么本脚本必须有它（v1.4.61，一枚会说假话的链上判据）：
    `tests/test_release_gates.py::test_every_chain_tool_can_prove_it_fails` 会拿
    `--self-test` 挨个跑发版链上的工具，rc==2 才算"没有可用自检"。本脚本原先不认这个参数，
    旧 main 的分支形状是「不是 --print / --check 就是写文件」，于是那次"自检"实际做的是
    **用一个没有邮箱的环境变量把真实的 ../.sf-opensource/docs/INSTALL.md 重写一遍** ——
    已经设好的求助邮箱被抹回占位符，而用例拿到 rc=0，报"链上工具全部能自证失败"。
    所以这里补三件事：① 真自检；② 未知参数一律退 2、绝不落到写入分支；③ 只碰内存与临时目录。

    只加一个空壳 flag 蒙不过去：那条用例判的是退出码，而这里每一条都种了真实的错形状。
    """
    ok, bad = 0, []

    def check(name: str, cond: bool) -> None:
        nonlocal ok
        if cond:
            ok += 1
        else:
            bad.append(name)

    # ① 字面 %s —— v1.4.59 那枚少写一步 % 运算的真事故
    check("种下字面 %s 必须报",
          any("%s" in p for p in lint_placeholders("双击 `Skyrocket-V%s.dmg` 即可")))
    # ② 联系渠道是死路：默认判红；显式放行只放这一条，不许连 %s 一起放
    dead = "请把截图发给（发版前把这里换成专用邮箱：%s）" % CONTACT_ENV
    check("种下未设邮箱必须报", any(CONTACT_ENV in p for p in lint_placeholders(dead)))
    cleared = lint_placeholders(dead, allow_unset_contact=True)
    check("显式放行后邮箱那条要消失", not any(CONTACT_ENV in p for p in cleared))
    # ③ 干净文本必须零告状（否则这是一道永不失败的检查）
    check("干净文本必须放行",
          lint_placeholders("双击 Skyrocket-V1.4.61.dmg，发给 support@example.com") == [])
    # ④ 渲染本体：设了邮箱就不许留占位符，DMG 名必须是真实版本号形状
    saved = os.environ.get(CONTACT_ENV)
    os.environ[CONTACT_ENV] = "support@example.com"
    real_sha = globals()["manifest_sha256"]
    globals()["manifest_sha256"] = lambda: "ab" * 32
    try:
        text = render()
    finally:
        globals()["manifest_sha256"] = real_sha
        if saved is None:
            os.environ.pop(CONTACT_ENV, None)
        else:
            os.environ[CONTACT_ENV] = saved
    check("渲染产物不许留 %s", "%s" not in text)
    check("渲染产物要写出真实 DMG 名", re.search(r"Skyrocket-V\d+\.\d+\.\d+\.dmg", text) is not None)
    # 双击那一步必须按**卷里看得见的名字**写。磁盘上的真名带着 `.command`（那是终端认它
    # 的依据），而发版链第 4 步会把后缀从显示里去掉 —— 说明里写「双击 一键安装.command」
    # 等于让用户在一个界面上不存在的名字里找。切法走 AIC 那份实现，两边不会各说各话。
    real_name = launcher_script().name
    shown_name = AIC.volume_displayed_name(real_name)
    check("双击指引用的是显示名", ("双击 `%s`" % shown_name) in text)
    check("双击指引里不许出现磁盘真名（带后缀那枚）", ("双击 `%s`" % real_name) not in text)
    # 上面两条必须"有东西可断"：若哪天显示名与真名相同（隐藏后缀那一步被关掉），
    # 这两条会同时变成空转 —— 所以这里显式钉住"显示名确实是真名去掉一段"。
    check("前两条不是空转：显示名确实比磁盘真名短，且是它的前缀",
          shown_name != real_name and real_name.startswith(shown_name))
    check("渲染产物用设好的邮箱，且不外泄变量名",
          "support@example.com" in text and CONTACT_ENV not in text)
    check("渲染后 lint 必须空", lint_placeholders(text) == [])
    # ⑤ 未知参数：退 2，且绝不写文件（这一条就是本轮那枚假门的反面）
    tmp = tempfile.mkdtemp(prefix="sf_guide_selftest_")
    out_path = Path(tmp) / "docs" / "INSTALL.md"
    real_out = globals()["OUT_DEFAULT"]
    globals()["OUT_DEFAULT"] = out_path
    try:
        check("未知参数必须退 2", main(["render_install_guide.py", "--definitely-not-a-flag"]) == 2)
        check("未知参数不得写文件", not out_path.exists())
        # 裸位置参数是同一个洞的另一半（F5）：只过滤带 -- 的写法拦不住它
        check("漏了横杠的参数（check）也必须退 2",
              main(["render_install_guide.py", "check"]) == 2)
        check("漏横杠时同样不得写文件", not out_path.exists())
        # ⑥ 写入侧被种了 %s 时必须拒绝落盘
        broken = "# 安装说明\n1. 双击 `Skyrocket-V%s.dmg`；\n"
        real_render = globals()["render"]
        globals()["render"] = lambda: broken
        try:
            check("正文留 %s 必须拒绝写入", main(["render_install_guide.py"]) == 1)
        finally:
            globals()["render"] = real_render
        check("拒绝写入时不许留下文件", not out_path.exists())
    finally:
        globals()["OUT_DEFAULT"] = real_out
        shutil.rmtree(tmp, ignore_errors=True)
    print("== render_install_guide 自检：%d 项通过，%d 项失败 ==" % (ok, len(bad)))
    if bad:
        print("!! 失败项：" + "、".join(bad), file=sys.stderr)
        return 1
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv if argv is None else argv)
    known = ("--print", "--check", "--allow-unset-contact", "--self-test", "--local-only")
    # 不看前缀（2026-10-12 全方位复核 F5）：旧写法只挑带 -- 的参数来判未知，
    # 于是 `render_install_guide.py check`（漏两个横杠）会被当成"没匹配到 --print/--check
    # 就是写文件"，直接重写仓库外那份真实对外 INSTALL.md —— 与 G17 那枚假门同一形状，
    # 只是入口从"未知开关"换成了"裸位置参数"。本工具没有任何合法位置参数。
    unknown = [a for a in argv[1:] if a not in known]
    if unknown:
        # 这一条不是洁癖：v1.4.61 实测，发版链上的用例给本脚本传了它不认识的
        # `--self-test`，旧实现按"没匹配到 --print/--check 就是写文件"处理，
        # 于是**用空环境变量把真实的对外 INSTALL.md 重写了一遍**，把已经设好的
        # 求助邮箱抹回占位符，而那枚用例拿到 rc=0、报"自检通过"。
        # 未知参数必须退 2（用例判"没有可用自检"正是看 rc==2），绝不许落到写入分支。
        print("!! 不认识参数：%s\n   可用：--print / --check / --allow-unset-contact / --self-test"
              % " ".join(unknown), file=sys.stderr)
        return 2
    if "--self-test" in argv:
        return self_test()
    if "--print" in argv:
        print(render(local_only=True) if "--local-only" in argv else render())
        return 0
    out = OUT_DEFAULT
    if "--check" in argv:
        if not out.is_file():
            print("!! 安装说明不存在：%s（发版前必须渲染一次）" % out, file=sys.stderr)
            return 1
        body = out.read_text(encoding="utf-8")
        if "--local-only" in argv:
            problems = lint_commands(body) + lint_placeholders(body)
            if body != render(local_only=True):
                problems.append("本机说明与当前版本不同源，或混入未发布的线上下载步骤")
            if problems:
                print("!! " + "; ".join(problems), file=sys.stderr)
                return 1
            print("== 本机安装说明同源：v%s，不提供未发布下载链接 ==" % APP_VERSION)
            return 0
        problems = lint_commands(body) + lint_placeholders(
            body, allow_unset_contact="--allow-unset-contact" in argv)
        for token, why in ((download_url(), "那行 curl 命令的下载地址不是本轮版本"),
                           (archive_name(), "包名不是本轮版本"),
                           (checksums_name(), "校验清单不是本轮版本")):
            if token not in body:
                problems.append(why)
        digest = manifest_sha256()
        if body != render():
            problems.append("安装说明与当前版本的稳定渲染内容不一致")
        # 线上可达性：说明指向的包必须真的在架上。拿不到结论时只如实说明，不假装通过也不判红。
        state, note = probe_asset(download_url())
        if state == "missing":
            problems.append("那行命令指向的包在线上还不存在（%s）—— "
                            "这份说明现在不该发出去，用户照抄会 404" % note)
        elif state == "unknown":
            print("   ⊘ 线上可达性未核验：%s" % note)
        else:
            print("   线上资产可达：%s" % note)
        if problems:
            print("!! 安装说明有问题（v%s）：" % APP_VERSION, file=sys.stderr)
            for one in problems:
                print("   · " + one, file=sys.stderr)
            print("   重跑：./.venv/bin/python tools/render_install_guide.py", file=sys.stderr)
            return 1
        print("== 安装说明与本轮版本同源（v%s，独立校验清单，资产摘要%s，命令块无 sudo/删除）=="
              % (APP_VERSION, "已生成" if digest else "未生成"))
        return 0
    out.parent.mkdir(parents=True, exist_ok=True)
    text = render(local_only=True) if "--local-only" in argv else render()
    # v1.4.60（复核 F3）：写入这一侧也过占位符判据 —— 字面 %s 属于"写出去就是错的"，
    # 直接拒绝写入；联系渠道占位允许先写出来（下面照旧提示），但 `--check` 会判红，
    # 所以它进不了发版链第 6.1 步之后的任何一次核验。
    bad = lint_commands(text) + lint_placeholders(text, allow_unset_contact=True)
    if bad:
        print("!! 渲染出来的命令自己就不过 FR-12 的约束，不写入：", file=sys.stderr)
        for one in bad:
            print("   · " + one, file=sys.stderr)
        return 1
    out.write_text(text, encoding="utf-8")
    read_back = out.read_text(encoding="utf-8")
    if read_back != text:
        print("!! 写入后读回不一致：%s" % out, file=sys.stderr)
        return 1
    print("已写入 %s（%d 字，v%s）" % (out, len(text), APP_VERSION))
    return 0


if __name__ == "__main__":
    sys.exit(main())
