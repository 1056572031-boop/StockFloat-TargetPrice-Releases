#!/usr/bin/env python3
"""桌面分发目录（~/Desktop/Skyrocket）与本轮发布的对账与同步。

为什么需要它：
  发版链只会往这个目录写三样东西（DMG、更新 ZIP、Skyrocket-update.json）。目录里其余文件
  没有任何一步会碰，它们的旧只会越攒越旧，而且不报错。2026-10-22 实测：一份说明文档停在
  10 月 10 日，正文里还写着一键安装脚本的旧名字；旧一代清单停在 1.4.54。
  本工具把「目录里每个文件该怎么跟着版本走」写成登记表，并在发版后核对。

三类文件
  版本绑定件  Skyrocket-V<版本>.dmg / Skyrocket-<版本>-macos.zip / Skyrocket-update.json
              必须是本轮的；清单里的大小、哈希、地址要与桌面上那份 ZIP 对得上；旧版残留判红。
  冻结件      StockFloatTargetPrice-update.json 故意停在 1.4.54（旧一代客户端按包标识读它，
              一份清单喂不了两代程序）。被刷新成本版反而是错，同样判红。
  同步文档    含 SF-RELEASE-SYNC 标记段的 .md：发版时只改写标记段（对账版本、发布时间、
              一键安装脚本现名、正文里仓库已不存在的引用），正文一个字都不动。

用法（未知参数与缺少动作一律退 2，不会落到默认动作）
    ./.venv/bin/python tools/desktop_folder_sync.py --check
    ./.venv/bin/python tools/desktop_folder_sync.py --refresh [--adopt 文件名.md]
    ./.venv/bin/python tools/desktop_folder_sync.py --self-test
退出码  0 一致；1 有硬问题；2 参数错误；3 一致，但文档正文里有待人工订正的失效引用。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sf_constants import APP_VERSION, BUNDLE_ID, DESKTOP_OUTPUT_DIR  # noqa: E402

BEGIN = "<!-- SF-RELEASE-SYNC:BEGIN -->"
END = "<!-- SF-RELEASE-SYNC:END -->"
BLOCK_RE = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END), re.S)
CURRENT_JSON = "Skyrocket-update.json"
# 旧一代清单故意冻结：文件名 -> 它必须停在的版本。
FROZEN = {"StockFloatTargetPrice-update.json": "1.4.54"}
# 运行期才出现的文件名，不在仓库里是正常的，不算失效引用。
RUNTIME_NAMES = frozenset({"config.json", "features.ini", "result.json", "debug.log", "state.json"})
SKIP_DIRS = frozenset({".git", ".venv", "build", "dist", "backups", "node_modules", "__pycache__"})
PATHLIKE = re.compile(r"^(?:[\w.\-]+/)*[\w.\-]+\.(?:py|sh|command|spec|json|md|txt|ini)$")
OWN_FAMILY = re.compile(r"^(Skyrocket-V.*\.dmg|Skyrocket-.*-macos\.zip|"
                        r"StockFloatTargetPrice-.*-macos\.zip|实时股价-目标价版-V.*\.dmg)$")


class Report:
    """核对结果：problems 是硬问题（退 1），notes 只提示，stale 是待人工订正的失效引用（退 3）。"""

    def __init__(self) -> None:
        self.problems: list[str] = []
        self.notes: list[str] = []
        self.stale: list[str] = []

    def code(self) -> int:
        if self.problems:
            return 1
        return 3 if self.stale else 0


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def repo_names(repo: Path) -> tuple[set[str], set[str]]:
    """仓库里现存文件的相对路径集合与文件名集合（跳过环境与产物目录）。"""
    rels: set[str] = set()
    names: set[str] = set()
    for base, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            rels.add(os.path.relpath(os.path.join(base, name), repo))
            names.add(name)
    return rels, names


def find_stale(body: str, repo: Path) -> list[str]:
    """正文里写成 `路径或文件名` 的引用，仓库里已经找不到的那些。"""
    rels, names = repo_names(repo)
    stale: list[str] = []
    for token in re.findall(r"`([^`\n]+)`", body):
        base = token.split("::")[0]
        if base.startswith(("~", "/")) or " " in base or not PATHLIKE.match(base):
            continue
        if base in RUNTIME_NAMES or base in rels or Path(base).name in names:
            continue
        if base not in stale:
            stale.append(base)
    return stale


def installer_name(repo: Path) -> str:
    found = sorted(p.name for p in (repo / "installer").glob("*.command"))
    return found[0] if len(found) == 1 else "（installer/ 下不是恰好一枚 .command）"


def build_block(version: str, published: str, installer: str, stale: list[str]) -> str:
    lines = [BEGIN,
             "> 发版对账（由 tools/desktop_folder_sync.py 在每次发版时改写，请勿手改此段）",
             "> - 对账版本：%s" % version,
             "> - 本版发布时间：%s" % published,
             "> - 一键安装脚本现名：%s" % installer]
    if stale:
        lines.append("> - 正文里仓库已不存在、待人工订正的引用：" + "、".join("`%s`" % s for s in stale))
    else:
        lines.append("> - 正文里没有发现仓库已不存在的文件引用")
    lines.append("> - 说明：本段只核对版本号与文件名这类可机器判定的事实，不代表正文内容已逐句复核。")
    lines.append(END)
    return "\n".join(lines)


def split_doc(text: str) -> tuple[str, str | None, str]:
    """返回 (去掉标记段后的正文, 标记段, 状态)；状态 ok/none/broken。"""
    if text.count(BEGIN) == 0 and text.count(END) == 0:
        return text, None, "none"
    if text.count(BEGIN) != 1 or text.count(END) != 1 or not BLOCK_RE.search(text):
        return text, None, "broken"
    match = BLOCK_RE.search(text)
    assert match is not None
    return text[:match.start()] + text[match.end():], match.group(0), "ok"


def read_manifest(folder: Path, report: Report) -> dict | None:
    path = folder / CURRENT_JSON
    if not path.is_file():
        report.problems.append("缺少 %s" % CURRENT_JSON)
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        report.problems.append("%s 读不出来：%s" % (CURRENT_JSON, exc))
        return None
    return data if isinstance(data, dict) else None


def check_artifacts(folder: Path, version: str, report: Report) -> dict | None:
    dmg = folder / ("Skyrocket-V%s.dmg" % version)
    zpath = folder / ("Skyrocket-%s-macos.zip" % version)
    for path in (dmg, zpath):
        if not path.is_file() or path.stat().st_size == 0:
            report.problems.append("缺少或为空：%s" % path.name)
    for entry in sorted(folder.iterdir()):
        if OWN_FAMILY.match(entry.name) and entry.name not in (dmg.name, zpath.name):
            report.problems.append("旧版残留：%s（本轮应只剩 %s 与 %s）" % (entry.name, dmg.name, zpath.name))
    data = read_manifest(folder, report)
    if data is None:
        report.problems.append("%s 不可用，无法核对 ZIP" % CURRENT_JSON)
        return None
    if data.get("version") != version:
        report.problems.append("%s 的版本是 %s，应为 %s" % (CURRENT_JSON, data.get("version"), version))
    if data.get("bundle_id") != BUNDLE_ID:
        report.problems.append("%s 的包标识 %s 不是 %s" % (CURRENT_JSON, data.get("bundle_id"), BUNDLE_ID))
    archive = data.get("archive") or {}
    if zpath.is_file():
        if archive.get("size") != zpath.stat().st_size:
            report.problems.append("清单记的 ZIP 大小 %s 与桌面上那份 %d 对不上" % (archive.get("size"), zpath.stat().st_size))
        if archive.get("sha256") != sha256_of(zpath):
            report.problems.append("清单记的 ZIP 哈希与桌面上那份对不上")
        if not str(archive.get("url", "")).endswith("/" + zpath.name):
            report.problems.append("清单的下载地址没有指向 %s" % zpath.name)
    return data


def check_frozen(folder: Path, version: str, report: Report) -> None:
    for name, pinned in FROZEN.items():
        path = folder / name
        if not path.is_file():
            report.notes.append("冻结件 %s 不在目录里（不影响本轮）" % name)
            continue
        try:
            found = json.loads(path.read_text(encoding="utf-8")).get("version")
        except (OSError, ValueError, AttributeError) as exc:
            report.problems.append("冻结件 %s 读不出来：%s" % (name, exc))
            continue
        if found != pinned:
            report.problems.append("冻结件 %s 应停在 %s，现在是 %s（旧一代客户端按它读，不该被刷新）"
                                   % (name, pinned, found))
        elif found == version:
            report.problems.append("冻结件 %s 与本轮同版本，登记表需要复核" % name)


def published_of(data: dict | None) -> str:
    return str((data or {}).get("published_at") or "未知")


def check_docs(folder: Path, version: str, data: dict | None, repo: Path, report: Report) -> None:
    installer = installer_name(repo)
    for doc in sorted(folder.glob("*.md")):
        body, block, state = split_doc(doc.read_text(encoding="utf-8"))
        if state == "none":
            report.notes.append("%s 还没有对账标记段，发版时用 --refresh --adopt %s 登记一次" % (doc.name, doc.name))
            continue
        if state == "broken":
            report.problems.append("%s 的对账标记段残缺或重复" % doc.name)
            continue
        stale = find_stale(body, repo)
        expected = build_block(version, published_of(data), installer, stale)
        if block != expected:
            report.problems.append("%s 的对账段不是本轮的（版本、发布时间、脚本名或失效引用有变），需要 --refresh" % doc.name)
        for item in stale:
            report.stale.append("%s 引用了仓库里已不存在的 `%s`" % (doc.name, item))
    known = {p.name for p in folder.iterdir()}
    for name in sorted(known):
        if name.startswith(".") or name.endswith(".md") or OWN_FAMILY.match(name):
            continue
        if name != CURRENT_JSON and name not in FROZEN:
            report.notes.append("目录里有未登记的文件：%s（该冻结、同步还是删，写进登记表）" % name)


def audit(folder: Path, version: str, repo: Path) -> Report:
    report = Report()
    if not folder.is_dir():
        report.problems.append("分发目录不存在：%s" % folder)
        return report
    data = check_artifacts(folder, version, report)
    check_frozen(folder, version, report)
    check_docs(folder, version, data, repo, report)
    return report


def write_atomic(path: Path, text: str, backup_dir: Path) -> None:
    backup_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, backup_dir / path.name)
    tmp = path.with_name(path.name + ".sync-tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def sync_doc(doc: Path, block_for, backup_dir: Path, adopt: bool) -> str:
    """改写一份文档的标记段；返回 unchanged / updated / adopted / refused。"""
    text = doc.read_text(encoding="utf-8")
    body, block, state = split_doc(text)
    if state == "broken" or (state == "none" and not adopt):
        return "refused"
    new_block = block_for(body)
    if state == "ok":
        match = BLOCK_RE.search(text)
        assert match is not None
        new_text = text[:match.start()] + new_block + text[match.end():]
    else:
        lines = text.split("\n")
        at = next((i + 1 for i, ln in enumerate(lines) if ln.startswith("#")), 0)
        lines[at:at] = ["", new_block]
        new_text = "\n".join(lines)
    if new_text == text:
        return "unchanged"
    write_atomic(doc, new_text, backup_dir)
    return "adopted" if state == "none" else "updated"


def refresh(folder: Path, version: str, repo: Path, adopt: list[str], backup_dir: Path) -> int:
    """先核对版本绑定件与冻结件，全部过了才改写文档对账段；返回值与 --check 同口径。"""
    gate = Report()
    if not folder.is_dir():
        print("!! 分发目录不存在：%s" % folder, file=sys.stderr)
        return 1
    data = check_artifacts(folder, version, gate)
    check_frozen(folder, version, gate)
    if gate.problems:
        print_report(gate, "拒绝改写文档（发布件本身不对，盖章只会把错误写成对账）")
        return 1
    for name in adopt:
        if Path(name).name != name or not name.endswith(".md") or not (folder / name).is_file():
            print("!! --adopt 只收分发目录里的 .md 文件名：%s" % name, file=sys.stderr)
            return 2
    published, installer = published_of(data), installer_name(repo)
    broken = False
    for doc in sorted(folder.glob("*.md")):
        state = split_doc(doc.read_text(encoding="utf-8"))[2]
        if state == "broken":
            print("!! %s 的对账标记段残缺或重复，请手工修复后再同步" % doc.name, file=sys.stderr)
            broken = True
            continue
        if state == "none" and doc.name not in adopt:
            continue
        result = sync_doc(doc, lambda body: build_block(version, published, installer, find_stale(body, repo)),
                          backup_dir, doc.name in adopt)
        print("   %s：%s" % (doc.name, result))
    if broken:
        return 1
    final = audit(folder, version, repo)
    print_report(final, "同步后复核")
    return final.code()


def print_report(report: Report, title: str) -> None:
    print("== 桌面分发目录对账：%s ==" % title)
    for line in report.problems:
        print("!! " + line)
    for line in report.stale:
        print("?? " + line + "（正文需人工订正，工具不替你改）")
    for line in report.notes:
        print("-- " + line)
    if not (report.problems or report.stale or report.notes):
        print("   全部一致")


# ---- 自检：只碰内存与临时目录，绝不读写真实的桌面目录 ----
VER = "9.9.9"
DOC_BODY = "# 说明\n\n正文引用 `tools/real_tool.py`，还有运行期文件 `config.json`。\n"


def make_fixture(tmp: Path) -> tuple[Path, Path]:
    repo, folder = tmp / "repo", tmp / "Skyrocket"
    (repo / "tools").mkdir(parents=True)
    (repo / "installer").mkdir()
    (repo / "tools" / "real_tool.py").write_text("x\n", encoding="utf-8")
    (repo / "installer" / "一键安装.command").write_text("x\n", encoding="utf-8")
    folder.mkdir()
    zpath = folder / ("Skyrocket-%s-macos.zip" % VER)
    zpath.write_bytes(b"zip-bytes")
    (folder / ("Skyrocket-V%s.dmg" % VER)).write_bytes(b"dmg")
    manifest = {"version": VER, "bundle_id": BUNDLE_ID, "published_at": "2026-01-01T00:00:00Z",
                "archive": {"url": "https://example.invalid/d/" + zpath.name,
                            "size": zpath.stat().st_size, "sha256": sha256_of(zpath)}}
    (folder / CURRENT_JSON).write_text(json.dumps(manifest), encoding="utf-8")
    for name, ver in FROZEN.items():
        (folder / name).write_text(json.dumps({"version": ver}), encoding="utf-8")
    (folder / "notes.md").write_text(DOC_BODY, encoding="utf-8")
    return folder, repo


def quiet(func, *args) -> int:
    import contextlib
    import io
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return func(*args)


def adopted_fixture(tmp: Path) -> tuple[Path, Path]:
    folder, repo = make_fixture(tmp)
    code = quiet(refresh, folder, VER, repo, ["notes.md"], tmp / "bak")
    if code != 0:
        raise RuntimeError("夹具自身登记后应为 0，实际 %d" % code)
    return folder, repo


def edit_manifest(folder: Path, **changes: object) -> None:
    path = folder / CURRENT_JSON
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(changes)
    path.write_text(json.dumps(data), encoding="utf-8")


def edit_archive(folder: Path, **changes: object) -> None:
    path = folder / CURRENT_JSON
    data = json.loads(path.read_text(encoding="utf-8"))
    data["archive"].update(changes)
    path.write_text(json.dumps(data), encoding="utf-8")


def edit_doc(folder: Path, old: str, new: str) -> None:
    doc = folder / "notes.md"
    text = doc.read_text(encoding="utf-8")
    if old not in text:
        raise RuntimeError("夹具里找不到要改的文字：%s" % old)
    doc.write_text(text.replace(old, new, 1), encoding="utf-8")


def append_doc(folder: Path, extra: str) -> None:
    doc = folder / "notes.md"
    doc.write_text(doc.read_text(encoding="utf-8") + extra, encoding="utf-8")


def mutations(zip_name: str) -> list:
    """每一项种一处错，--check 的口径下都必须判红（退 1）。"""
    frozen = next(iter(FROZEN))
    return [
        ("旧版 DMG 残留", lambda f, r: (f / "Skyrocket-V9.9.8.dmg").write_bytes(b"o")),
        ("本轮 ZIP 缺失", lambda f, r: (f / zip_name).unlink()),
        ("ZIP 被换字节", lambda f, r: (f / zip_name).write_bytes(b"other")),
        ("ZIP 同长度换字节（只有哈希能抓）", lambda f, r: (f / zip_name).write_bytes(b"ZIP-BYTES")),
        ("清单记的大小不对（字节没动，只有大小能抓）", lambda f, r: edit_archive(f, size=1)),
        ("清单下载地址指向别的包", lambda f, r: edit_archive(f, url="https://example.invalid/d/other.zip")),
        ("清单版本落后", lambda f, r: edit_manifest(f, version="9.9.8")),
        ("清单包标识不对", lambda f, r: edit_manifest(f, bundle_id="x.y")),
        ("冻结清单被刷新成本版", lambda f, r: (f / frozen).write_text(json.dumps({"version": VER}), encoding="utf-8")),
        ("冻结清单被刷新成别的版本（只有钉死版本能抓）",
         lambda f, r: (f / frozen).write_text(json.dumps({"version": "9.9.7"}), encoding="utf-8")),
        ("对账段版本过期", lambda f, r: edit_doc(f, "对账版本：" + VER, "对账版本：9.9.8")),
        ("标记段缺结尾", lambda f, r: edit_doc(f, END, "")),
        ("正文新增失效引用", lambda f, r: append_doc(f, "\n见 `tools/gone.py`\n")),
    ]


class Tally:
    def __init__(self) -> None:
        self.total = 0
        self.failed: list[str] = []

    def expect(self, label: str, condition: bool) -> None:
        self.total += 1
        if not condition:
            self.failed.append(label)


def scratch() -> tempfile.TemporaryDirectory:
    return tempfile.TemporaryDirectory(prefix="sf_dfs_")


def selftest_audit(tally: Tally) -> None:
    zip_name = "Skyrocket-%s-macos.zip" % VER
    with scratch() as raw:
        folder, repo = adopted_fixture(Path(raw))
        tally.expect("干净基线 --check 应为 0", audit(folder, VER, repo).code() == 0)
        (folder / next(iter(FROZEN))).unlink()
        tally.expect("冻结件缺失只提示、不判红", audit(folder, VER, repo).code() == 0)
        (folder / "stray.bin").write_bytes(b"s")
        tally.expect("未登记文件只提示", any("stray.bin" in n for n in audit(folder, VER, repo).notes))
    for label, mutate in mutations(zip_name):
        with scratch() as raw:
            folder, repo = adopted_fixture(Path(raw))
            mutate(folder, repo)
            tally.expect("种错必红：" + label, audit(folder, VER, repo).code() != 0)


def body_lines(path: Path) -> list[str]:
    body = split_doc(path.read_text(encoding="utf-8"))[0]
    return [ln for ln in body.split("\n") if ln.strip()]


def selftest_refresh(tally: Tally) -> None:
    with scratch() as raw:
        tmp = Path(raw)
        folder, repo = adopted_fixture(tmp)
        doc = folder / "notes.md"
        before = body_lines(doc)
        edit_doc(folder, "对账版本：" + VER, "对账版本：9.9.8")
        tally.expect("过期对账段 --check 红", audit(folder, VER, repo).code() != 0)
        tally.expect("--refresh 修复后退 0", quiet(refresh, folder, VER, repo, [], tmp / "bak") == 0)
        tally.expect("修复后 --check 绿", audit(folder, VER, repo).code() == 0)
        tally.expect("改写前留了备份", (tmp / "bak" / "notes.md").is_file())
        tally.expect("只改对账段、正文逐行不变", body_lines(doc) == before)
        once, saved = doc.read_bytes(), (tmp / "bak" / "notes.md").read_bytes()
        quiet(refresh, folder, VER, repo, [], tmp / "bak")
        tally.expect("重复 --refresh 幂等", doc.read_bytes() == once)
        tally.expect("空跑不覆盖改前的备份", (tmp / "bak" / "notes.md").read_bytes() == saved)
    with scratch() as raw:
        tmp = Path(raw)
        folder, repo = adopted_fixture(tmp)
        (folder / ("Skyrocket-%s-macos.zip" % VER)).write_bytes(b"tampered")
        edit_doc(folder, "对账版本：" + VER, "对账版本：9.9.8")
        stale = (folder / "notes.md").read_bytes()
        tally.expect("发布件不对时拒绝盖章（退 1）", quiet(refresh, folder, VER, repo, [], tmp / "bak") == 1)
        tally.expect("拒绝时文档字节不动", (folder / "notes.md").read_bytes() == stale)
    with scratch() as raw:
        tmp = Path(raw)
        folder, repo = adopted_fixture(tmp)
        append_doc(folder, "\n见 `tools/gone.py`\n")
        tally.expect("正文有失效引用时退 3", quiet(refresh, folder, VER, repo, [], tmp / "bak") == 3)
        text = (folder / "notes.md").read_text(encoding="utf-8")
        tally.expect("失效引用写进对账段", "tools/gone.py" in text.split(END)[0])
        tally.expect("标记段之后的正文不被吞", text.rstrip().endswith("见 `tools/gone.py`"))


def selftest_edges(tally: Tally) -> None:
    with scratch() as raw:
        tmp = Path(raw)
        folder, repo = make_fixture(tmp)
        raw_doc = (folder / "notes.md").read_bytes()
        tally.expect("未登记文档只提示、不判红", audit(folder, VER, repo).code() == 0)
        quiet(refresh, folder, VER, repo, [], tmp / "bak")
        tally.expect("未 --adopt 的文档字节不动", (folder / "notes.md").read_bytes() == raw_doc)
        for bad in ("../x.md", "sub/x.md", "notes.txt", "missing.md"):
            tally.expect("非法 --adopt 退 2：" + bad, quiet(refresh, folder, VER, repo, [bad], tmp / "bak") == 2)
        tally.expect("目录不存在判红", audit(tmp / "nope", VER, repo).code() == 1)
        stale = find_stale("`tools/gone.py` `tools/real_tool.py::Case` `config.json` `~/a.py`", repo)
        tally.expect("失效引用判定", stale == ["tools/gone.py"])
        tally.expect("脚本现名取自 installer/", installer_name(repo) == "一键安装.command")
    with scratch() as raw:
        tmp = Path(raw)
        folder, repo = adopted_fixture(tmp)
        edit_doc(folder, END, "")
        tally.expect("标记段残缺时 --refresh 拒绝（退 1）", quiet(refresh, folder, VER, repo, [], tmp / "bak") == 1)


def self_test() -> int:
    tally = Tally()
    for case in (selftest_audit, selftest_refresh, selftest_edges):
        case(tally)
    if tally.failed:
        for label in tally.failed:
            print("自检未通过：" + label, file=sys.stderr)
        print("== 自检失败 %d/%d ==" % (len(tally.failed), tally.total), file=sys.stderr)
        return 1
    print("== 自检通过：%d 项断言，其中 %d 处种错全部被抓到 ==" % (tally.total, len(mutations("z"))))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="桌面分发目录与本轮发布的对账与同步")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true", help="只读核对")
    action.add_argument("--refresh", action="store_true", help="核对通过后改写文档对账段")
    action.add_argument("--self-test", action="store_true", help="变异自检（只碰临时目录）")
    parser.add_argument("--adopt", action="append", default=[], metavar="NAME.md", help="首次登记一份文档（配合 --refresh）")
    parser.add_argument("--dir", type=Path, default=Path(DESKTOP_OUTPUT_DIR))
    parser.add_argument("--version", default=APP_VERSION)
    parser.add_argument("--repo", type=Path, default=ROOT)
    parser.add_argument("--backup-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    if args.self_test:
        return self_test()
    if args.adopt and not args.refresh:
        parser.error("--adopt 只能和 --refresh 一起用")
    if args.check:
        report = audit(args.dir, args.version, args.repo)
        print_report(report, "核对 v%s" % args.version)
        return report.code()
    backup = args.backup_dir or args.repo / "build" / "_prev" / ("desktop_docs_" + time.strftime("%Y%m%d_%H%M%S"))
    return refresh(args.dir, args.version, args.repo, args.adopt, backup)


if __name__ == "__main__":
    sys.exit(main())
