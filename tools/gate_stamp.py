#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归关卡时间戳（v1.4.59 起，S7-② 的落点）。

要补的洞（30 轮走查 S7 第②项，实测结论）：build_release.sh 从第 1 步版本号一路走到
第 6 步对外发 OTA，**全程没有调用过 run_tests.sh / static_gate**。所有产物核验都排在
打包之后，而打包本身不可逆：DMG 会覆盖桌面、/Applications 会被替换、OTA 会把线上清单
指针切到这一版。也就是说此前任何一轮都可以「不跑回归直接发版」，而且零报错。

做法：run_tests.sh 全绿时在 build/.gate_stamp 落一枚时间戳，记录「刚才跑过回归的那份
代码是什么」；build_release.sh 在打包**之前**比对时间戳与当前工作区，不一致就先补跑
回归（只有显式 --skip-tests 才放行，并且大声警告）。

为什么判据是**内容指纹**而不是 HEAD：
  · HEAD 变了但代码字节没变（只提交文档、只提交发布说明）→ 进包的东西一模一样，
    为此重跑两分钟回归是噪音；
  · HEAD 没变但工作区被改了 → 那才真正会进包，必须判为过期。
所以 head 只是「这一轮当时对应哪次提交」的留档信息，判定只看 fingerprint。
未提交的改动同样进包，因此指纹覆盖工作区里**未跟踪**的新文件
（git ls-files -c -o --exclude-standard，而不是只看索引）。

为什么把文件权限也编进指纹：只 chmod +x 一个 shell 资源时字节不变，纯内容哈希看不出来，
而那是会进包的行为差异。早先掺过 `git status --porcelain` 整段文本，结果**只改一个 md
文档**都会让时间戳判过期 —— 掺全文把范围外文件的动静也算进来了，已改成逐文件自带权限。

指纹范围刻意只包含「能改变包内行为或改变关卡结论」的字节（见 BUILD_INPUT_SUFFIXES /
BUILD_INPUT_NAMES）。文档、图片、发布说明这类纯文字交付物不在其中：它们不进包，也不被
回归用例读取，把它们算进来就等于每次更新 DELIVERY_CHECKLIST.md 都白白重跑一整轮回归。
它们各自有闸门（3.5 版本、3.6 特征、3.7 复盘），不靠这枚时间戳。
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
STAMP_REL = os.path.join("build", ".gate_stamp")

# 会被 PyInstaller 收进包里、或被关卡自己读进判据的文件后缀。
BUILD_INPUT_SUFFIXES = (".py", ".sh", ".spec", ".command", ".otf", ".ttf",
                        ".yml", ".yaml", ".icns")
# 按后缀收不到、但确实影响构建或关卡结论的文件名。
BUILD_INPUT_NAMES = ("requirements.txt", "requirements.lock", "NOTICE.md", "README.md",
                     "INSTALL.md", "SECURITY.md", "LICENSE", ".coveragerc",
                     "static_gate_baseline.json", "test_discipline_baseline.json",
                     "escapes_ledger.json")

NO_GIT = "no-git"


def git_env() -> dict:
    """给子进程 git 用的环境：**剔掉所有 GIT_* 变量**。

    为什么必须剔：这道闸门是在 `git-hooks/pre-commit` 里跑的，而钩子环境带着
    `GIT_DIR` / `GIT_INDEX_FILE` / `GIT_PREFIX` 指向**被提交的那棵树**。自检与用例要
    在临时目录里 `git init` 造一个假仓库，那些变量一继承过去，临时仓库里的命令就全
    打到真仓库上 —— 实测钩子里跑 11 项自检只剩 9 过（种进去的错"没被抓到"），
    4 条用例同因判红。也就是说：**不在钩子里跑过一遍，这条闸门自己就是假门**。
    """
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def _git(args: list[str], cwd: str) -> str:
    """跑 git；不在仓库里/命令失败一律返回 NO_GIT（宁可算不出，也不猜一个值）。"""
    try:
        p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                           env=git_env())
    except OSError:
        return NO_GIT
    if p.returncode != 0:
        return NO_GIT
    return p.stdout


def git_head(cwd: str = PROJ) -> str:
    return _git(["rev-parse", "HEAD"], cwd).strip() or NO_GIT


def build_input_paths(cwd: str) -> list[str]:
    """进入指纹范围的相对路径：已跟踪 ∪ 未跟踪且未被忽略，且落在构建输入名单内。"""
    out = _git(["ls-files", "-c", "-o", "--exclude-standard", "-z"], cwd)
    if out == NO_GIT:
        return []
    paths = []
    for raw in out.split("\0"):
        if not raw:
            continue
        if raw.endswith(BUILD_INPUT_SUFFIXES) or \
           os.path.basename(raw) in BUILD_INPUT_NAMES:
            paths.append(raw)
    return sorted(set(paths))


def file_token(path: str) -> str:
    """单个文件的指纹 token：内容哈希 + 权限位 + 是否符号链接。"""
    try:
        st = os.lstat(path)
    except OSError:
        return "missing"
    if os.path.islink(path):
        try:
            target = os.readlink(path)
        except OSError:
            target = "?"
        return "link:" + target
    try:
        with open(path, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return "unreadable"
    return "%04o:%s" % (st.st_mode & 0o777, digest)


def content_fingerprint(cwd: str) -> str:
    """全部构建输入文件的 (权限, 内容) 聚合哈希，取 sha256 前 32 位。"""
    h = hashlib.sha256()
    for rel in build_input_paths(cwd):
        h.update(rel.encode("utf-8", "surrogateescape"))
        h.update(b"\0")
        h.update(file_token(os.path.join(cwd, rel)).encode("utf-8", "replace"))
        h.update(b"\n")
    return h.hexdigest()[:32]


def resolve_root(cwd: str | None = None) -> str:
    """时间戳落在**当前工作目录所在的 git 顶层**，而不是脚本自己所在的目录。

    为什么必须是 cwd：写时间戳这件事的含义是「这一棵树跑过回归」。按脚本位置算的话，
    一份从仓库复制出去的副本（实机核验、临时 worktree、测试用的临时仓库）跑一次会把
    戳记到**主树**上去 —— 主树随即被 build_release 认为「已经绿了」，而它什么都没跑。
    这正是本仓反复在防的「闸门自己说假话」，测试里真就撞出来过一次。
    按 git 顶层解析还顺带解决「在 tools/ 子目录里手动跑」算出不同指纹的问题。
    不在 git 仓库里时退回 cwd，让 files 归零、由 check() 拒绝放行。
    """
    base = cwd or os.getcwd()
    top = _git(["rev-parse", "--show-toplevel"], base).strip()
    if top and top != NO_GIT and os.path.isdir(top):
        return top
    return base


def stamp_path(root: str | None = None) -> str:
    return os.path.join(root or resolve_root(), STAMP_REL)


def current_info(root: str | None = None) -> dict:
    root = root or resolve_root()
    return {
        "head": git_head(root),
        "fingerprint": content_fingerprint(root),
        "files": len(build_input_paths(root)),
    }


def write_stamp(root: str | None = None) -> dict:
    """落一枚时间戳：内容 = head（留档）+ fingerprint（判据）+ 时间。"""
    root = root or resolve_root()
    info = dict(current_info(root))
    info["at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    path = stamp_path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False)
    return info


def read_stamp(root: str | None = None) -> dict | None:
    path = stamp_path(root)
    try:
        with open(path, encoding="utf-8") as f:
            obj = json.load(f)
    except (OSError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def verdict(stamp: dict | None, current: dict) -> tuple[bool, str]:
    """比对时间戳与当前代码，返回 (是否可用, 一句话理由)。判据只有 fingerprint。"""
    if stamp is None:
        return False, "没有关卡时间戳（从未跑过 ./run_tests.sh，或 build/ 已被清空）"
    got = str(stamp.get("fingerprint") or "")
    if not got:
        return False, "关卡时间戳里没有 fingerprint 字段（旧格式或被改写），不算数"
    if got != current["fingerprint"]:
        return False, "关卡时间戳与当前代码不同源：跑过回归之后构建输入又改了"
    return True, "关卡时间戳与当前代码同源（跑于 %s，当时 HEAD %s）" % (
        stamp.get("at", "?"), str(stamp.get("head", "?"))[:9])


def check(root: str | None = None) -> tuple[bool, str]:
    """时间戳可用性判定。空构建输入清单一律不可用：一条「什么都没算」的时间戳
    证明不了任何事，而它偏偏会和自己算出来的空指纹自洽通过（不在 git 仓库里、
    或整个树被挪进临时目录时会走到这里）。"""
    current = current_info(root)
    if current["files"] == 0:
        return False, "工作区取不到任何构建输入（不是 git 仓库？），时间戳无法核验"
    return verdict(read_stamp(root), current)


def self_test() -> int:
    """变异自检：种的错必须被抓到，正当发版不许被误判。

    全部跑在临时 git 仓库里 —— 真实工作区任何改动都会污染本轮判定（时间戳会立刻判过期，
    下一次发版就变成「不知道为什么补跑了一遍回归」）。
    """
    ok = 0
    bad = 0
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="sf_gate_stamp_") as tmp:
        def git(*args: str) -> None:
            subprocess.run(["git", *args], cwd=tmp, capture_output=True, text=True,
                           env=git_env())

        def write(name: str, text: str) -> None:
            with open(os.path.join(tmp, name), "w", encoding="utf-8") as f:
                f.write(text)

        def expect(name: str, want: bool) -> None:
            nonlocal ok, bad
            got, reason = check(tmp)
            if got is want:
                ok += 1
                print("  [OK] %s —— %s" % (name, reason))
            else:
                bad += 1
                failures.append(name)
                print("  [!!] %s —— 期望 %s，实得 %s：%s" % (name, want, got, reason))

        git("init", "-q")
        git("config", "user.email", "gate@example.invalid")
        git("config", "user.name", "gate")
        write("a.py", "X = 1\n")
        write("AGENTS.md", "# doc\n")
        git("add", "-A")
        git("commit", "-qm", "init")

        # ① 从没跑过回归 → 必须拦住（本关存在的意义）
        expect("没跑过回归", False)
        write_stamp(tmp)
        # ② 正向对照：刚跑完必须算数，否则就是一道每轮必红的关
        expect("跑完立刻判", True)
        # ③ 跑完后改了进包的 .py → 必须判过期
        write("a.py", "X = 2\n")
        expect("跑完后改了代码", False)
        write_stamp(tmp)
        # ④ 跑完后只改文档 → 不许重跑（文档不进包、用例也不读它）
        write("AGENTS.md", "# doc changed\n")
        expect("跑完后只改文档", True)
        # ⑤ 跑完后只提交、字节没变 → 不许判过期（HEAD 不是判据）
        git("add", "-A")
        git("commit", "-qm", "docs")
        expect("跑完后只提交", True)
        write_stamp(tmp)
        # ⑥ 新增**未跟踪**的 .py → 一样会进包，必须判过期
        write("new_tool.py", "Y = 1\n")
        expect("跑完后新增未跟踪 .py", False)
        write_stamp(tmp)
        # ⑦ 只 chmod 不改字节 → 靠把权限编进指纹抓到
        os.chmod(os.path.join(tmp, "a.py"), 0o755)
        expect("跑完后只 chmod", False)
        # ⑧ 时间戳是半截文件 / 非 JSON → 不算数，不许当成通过
        p = stamp_path(tmp)
        write(os.path.relpath(p, tmp), "{broken")
        expect("时间戳是半截文件", False)
        write_stamp(tmp)
        write(os.path.relpath(p, tmp), '{"head": "abc"}')
        expect("时间戳缺 fingerprint", False)
        # ⑨ 取不到构建输入（例如不是 git 仓库、或整个树被挪走）→ 即便时间戳就在
        #    那里、指纹和自己算出来的空集合完全自洽，也必须判不可用。
        outside = os.path.join(tmp, "not_a_repo")
        os.makedirs(outside, exist_ok=True)
        write_stamp(outside)
        empty, why = check(outside)
        if empty:
            bad += 1
            failures.append("空清单被判为可用")
            print("  [!!] 非仓库目录 —— 空清单竟然算通过：%s" % why)
        else:
            ok += 1
            print("  [OK] 非仓库目录 —— 空清单不放行（%s）" % why)
        if current_info(outside)["files"] != 0:
            bad += 1
            failures.append("非仓库目录仍算出文件数")
            print("  [!!] 非仓库目录不该算出构建输入文件")
        else:
            ok += 1
            print("  [OK] 非仓库目录 —— 构建输入为 0，发版侧据此拒绝放行")

    # 未知参数（2026-10-12 全方位复核 F5）：拼错的开关与打错的动词一律不许落到
    # 默认动作上 —— 这一枚的默认动作是"落一枚关卡时间戳"，放行等于跳过整轮回归。
    for argv, why in ((["write", "--qeuet"], "拼错的 --quiet"),
                      (["wri"], "打错的动词"),
                      (["write", "check"], "同时给两个动词"),
                      (["check", "--self-test-x"], "带 -- 的未知开关")):
        if unknown_args(argv):
            ok += 1
            print("  [OK] 未知参数（%s）—— 拒绝执行，退 2" % why)
        else:
            bad += 1
            failures.append("未知参数(%s) 被放行" % why)
            print("  [!!] 未知参数（%s）竟然被放行 —— 会静默落戳或跑错动词" % why)
    for argv, why in ((["write", "--quiet"], "正当的静默落戳"),
                      (["check"], "正当的核验"), (["path"], "正当的取路径")):
        if unknown_args(argv):
            bad += 1
            failures.append("正当参数(%s) 被误拒" % why)
            print("  [!!] %s 被误判成未知：%s" % (why, " ".join(unknown_args(argv))))
        else:
            ok += 1
            print("  [OK] %s —— 放行" % why)

    print("== gate_stamp 自检：%d 项通过，%d 项失败 ==" % (ok, bad))
    if failures:
        print("!! 失败项：" + "、".join(failures), file=sys.stderr)
        return 1
    return 0


VERBS = ("write", "check", "path")
FLAGS = ("--self-test", "--quiet")


def unknown_args(argv: list[str]) -> list[str]:
    """把"人不认识的东西"挑出来 —— 纯函数，自检可直接喂参数而不触发任何动作。

    为什么这一枚此前会滑过去（2026-10-12 全方位复核 F5）：main() 只看 argv[0] 是不是
    那三个动词，其余参数一概不检查。于是 `write --qeuet` 照样**落一枚时间戳**，
    而 build_release.sh 第 0 步的判据正是"戳与当前代码同源就不补跑回归" ——
    一个拼错的开关换来的是"这轮没跑回归也照样出包"。AGENTS §3.1 那条
    "未知参数一律退 2，绝不许落到默认动作里"此前只对带 -- 的参数生效，
    而这一枚连 -- 都不检查。动词给两个也算未知：谁该执行没有答案。
    """
    verbs = [a for a in argv if a in VERBS]
    unknown = [a for a in argv if a not in VERBS and a not in FLAGS]
    if len(verbs) > 1:
        unknown.append("同时给了动词 " + "、".join(verbs))
    return unknown


def main(argv: list[str]) -> int:
    if "--self-test" in argv:
        return self_test()
    unknown = unknown_args(argv)
    if unknown:
        print("!! 不认识参数：%s\n   用法：tools/gate_stamp.py [write|check|path] "
              "[--self-test] [--quiet]\n   未知参数不退 2 就等于把拼错当成授权："
              "这一枚的默认动作是落关卡时间戳，发版侧看到它就跳过整轮回归。"
              % " ".join(unknown), file=sys.stderr)
        return 2
    if argv and argv[0] == "write":
        info = write_stamp()
        if "--quiet" not in argv:
            print("== 关卡时间戳已落：%s ==" % stamp_path())
            print("   fingerprint=%s head=%s（覆盖 %d 个构建输入）" % (
                info["fingerprint"], str(info["head"])[:9], info["files"]))
        return 0
    if argv and argv[0] == "path":
        print(stamp_path())
        return 0
    if argv and argv[0] == "check":
        ok, why = check()
        print(("== 关卡时间戳可用：" if ok else "!! 关卡时间戳不可用：") + why)
        return 0 if ok else 1
    print("用法：tools/gate_stamp.py [write|check|path] [--self-test]")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
