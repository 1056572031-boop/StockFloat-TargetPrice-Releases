#!/usr/bin/env python3
"""发布后独立下载、公开源码/CI、DMG 和装机启动读回；只读远端，不自动发布。"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import stat
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
import quality_evidence as evidence
import release_state
import sf_update
from release_identity import REPO, TAG, MANIFEST_PATH  # noqa: E402


def asset_url(name: str) -> str:
    return (f"https://github.com/{REPO}/releases/download/"
            f"{urllib.parse.quote(TAG, safe='')}/{urllib.parse.quote(name, safe='')}")


def api(path: str) -> Any:
    result = subprocess.run(["gh", "api", "repos/" + REPO + "/" + path], check=True,
                            capture_output=True, text=True, timeout=60, env=evidence.gate_stamp.git_env())
    return json.loads(result.stdout)


def download(url: str, target: Path) -> bytes:
    if target.exists():
        raise ValueError("不覆盖已有下载证据: " + str(target))
    request = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
    with urllib.request.urlopen(request, timeout=90) as response, target.open("xb") as stream:
        total = 0
        while chunk := response.read(1024 * 1024):
            total += len(chunk)
            if total > 512 * 1024 * 1024:
                raise ValueError("下载超过 512 MiB 上限")
            stream.write(chunk)
    return target.read_bytes()


def source_archive(raw: bytes, tree: dict[str, Any]) -> dict[str, bytes]:
    if tree.get("truncated"):
        raise ValueError("公开 Git 树不完整")
    expected = {item["path"]: item for item in tree["tree"] if item["type"] == "blob"}
    actual = {}
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        for item in archive.infolist():
            if item.is_dir():
                continue
            prefix = "Skyrocket-source/"
            name = item.filename.removeprefix(prefix)
            if (not item.filename.startswith(prefix) or name.startswith("/") or ".." in Path(name).parts
                    or name in actual or name not in expected):
                raise ValueError("源码 ZIP 路径、重复条目或文件集合不符")
            content = archive.read(item)
            digest = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()
            mode = item.external_attr >> 16
            want = expected[name]
            if digest != want["sha"]:
                raise ValueError("源码 ZIP 与公开 Git blob 不同: " + name)
            if stat.S_IFMT(mode) and (stat.S_ISLNK(mode) != (want["mode"] == "120000")
                    or (not stat.S_ISLNK(mode) and bool(mode & 0o111) != (want["mode"] == "100755"))):
                raise ValueError("源码 ZIP 权限或软链接不符: " + name)
            actual[name] = content
    if set(actual) != set(expected) or not actual:
        raise ValueError("源码 ZIP 文件不完整")
    return actual


def check_ci(commit: str) -> dict[str, Any]:
    runs = api("actions/workflows/python-ci.yml/runs?head_sha=" + commit + "&per_page=100")["workflow_runs"]
    matching = [item for item in runs if item.get("head_sha") == commit and item.get("head_branch") == "public-source"]
    if not matching:
        raise ValueError("公开提交还没有 CI 记录")
    latest = max(matching, key=lambda item: (item["run_number"], item.get("run_attempt", 1)))
    jobs = api(f"actions/runs/{latest['id']}/jobs?per_page=100")["jobs"]
    required = {"test-offscreen", "test-cocoa-native"}
    if (latest.get("status") != "completed" or latest.get("conclusion") != "success"
            or not required.issubset({item["name"] for item in jobs if item.get("conclusion") == "success"})):
        raise ValueError("公开 CI 两项任务尚未全部通过")
    return {"run_id": latest["id"], "head_sha": commit, "jobs": jobs,
            "runs": matching, "boundary": "保留当前可见运行与重试次数；成功重试不代表首次全绿"}


def check_dmg(path: Path, app: dict[str, Any]) -> None:
    parent = Path(tempfile.mkdtemp(prefix="sf_readback_mount_"))
    mount = parent / "volume"
    mount.mkdir()
    subprocess.run(["hdiutil", "attach", "-readonly", "-nobrowse", "-mountpoint", str(mount), str(path)],
                   check=True, capture_output=True, timeout=60)
    try:
        actual = evidence.artifact_token(mount / "Skyrocket.app")
        if actual != {key: value for key, value in app.items() if key != "path"}:
            raise ValueError("下载 DMG 卷内应用与候选不一致")
    finally:
        # On detach failure preserve the mount; never recursively remove a possibly mounted tree.
        subprocess.run(["hdiutil", "detach", str(mount)], check=True, capture_output=True, timeout=60)
        mount.rmdir()
        parent.rmdir()


def verify(root: Path, state_path: Path, publication: Path, out: Path, installed: Path) -> int:
    if out.exists():
        raise ValueError("不覆盖已有发布后读回；请使用新 --out 路径")
    state = release_state.validate(root, state_path, "released_pending_acceptance")
    receipt = evidence.load(publication)
    if receipt.get("source") != state["source"] or receipt.get("version") != state["version"]:
        raise ValueError("公开发布回执与候选不同源")
    version = state["version"]
    out.parent.mkdir(parents=True, exist_ok=True)
    downloads = Path(tempfile.mkdtemp(prefix="readback-", dir=out.parent))
    checks: dict[str, Any] = {}
    report = {"version": version, "source": state["source"], "artifacts": state["artifacts"],
              "generated_at": evidence.now(), "verified": False, "checks": checks,
              "publication": release_state.reference(publication), "downloads": str(downloads),
              "boundary": "下载与本机装机读回；不代表跨机、Gatekeeper 或未执行真实场景通过"}

    def observe(name, action):
        try:
            checks[name] = {"status": "passed", "observation": action()}
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, zipfile.BadZipFile,
                sf_update.UpdateError) as exc:
            checks[name] = {"status": "failed", "reason": str(exc)}

    def asset(name, expected):
        url = asset_url(name)
        raw = download(url, downloads / name)
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError("下载摘要不一致: " + name)
        return raw

    def ota():
        raw = download(f"https://raw.githubusercontent.com/{REPO}/main/{MANIFEST_PATH}", downloads / "online-manifest.json")
        info = sf_update.verify_manifest(raw)
        if raw != Path(state["artifacts"]["ota_manifest"]["path"]).read_bytes() or info.version != version:
            raise ValueError("权威 OTA 清单不是本候选")
        return {"signature_valid": True, "sha256": hashlib.sha256(raw).hexdigest()}

    def dmg():
        item = state["artifacts"]["dmg"]
        name = Path(item["path"]).name
        asset(name, item["content_sha256"])
        check_dmg(downloads / name, state["artifacts"]["app"])
        return {"mounted_app_matches": True}

    def public_source():
        item = receipt["source_zip"]
        raw = asset(Path(item["path"]).name, item["content_sha256"])
        commit = receipt["public_commit"]
        if api("git/ref/heads/public-source")["object"]["sha"] != commit:
            raise ValueError("公开分支已不是本次发布提交")
        files = source_archive(raw, api("git/trees/" + commit + "?recursive=1"))
        for name in ("README.md", "docs/INSTALL.md", "SECURITY.md", "NOTICE.md", "LICENSE"):
            online = api("contents/" + name + "?ref=main")
            if base64.b64decode(online["content"]) != files[name]:
                raise ValueError("主分支公开资料与源码包不一致: " + name)
        return {"commit": commit, "file_count": len(files)}

    def checksums():
        item = receipt["checksums"]
        raw = asset(Path(item["path"]).name, item["content_sha256"])
        expected = {Path(value["path"]).name: value["content_sha256"] for value in
                    (state["artifacts"]["dmg"], state["artifacts"]["zip"], receipt["source_zip"])}
        entries = [line.split() for line in raw.decode("ascii").splitlines() if line.strip()]
        if len(entries) != len(expected) or any(len(entry) != 2 for entry in entries):
            raise ValueError("公开校验文件条目错误")
        if {entry[1]: entry[0] for entry in entries} != expected:
            raise ValueError("公开校验文件与发布资产不一致")
        return {"files": expected}

    def startup():
        from smoke_frozen_app import run_smoke
        if evidence.artifact_token(installed) != {key: value for key, value in state["artifacts"]["app"].items() if key != "path"}:
            raise ValueError("最终装机应用与候选不同源")
        subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(installed)],
                       check=True, capture_output=True, timeout=60)
        rc, result = run_smoke(installed, platform="cocoa", timeout=30, keep_dir=True)
        if rc != 0:
            raise ValueError("最终装机启动未通过: " + json.dumps(result))
        return result

    observe("ota_manifest", ota)
    observe("zip", lambda: {"size": len(asset(Path(state["artifacts"]["zip"]["path"]).name,
                                             state["artifacts"]["zip"]["content_sha256"]))})
    observe("dmg", dmg)
    observe("public_source", public_source)
    observe("checksums", checksums)
    observe("public_ci", lambda: check_ci(receipt["public_commit"]))
    observe("final_startup", startup)
    release_state.validate(root, state_path, "released_pending_acceptance")
    report["downloaded_files"] = {path.name: release_state.reference(path) for path in downloads.iterdir()}
    report["verified"] = all(item["status"] == "passed" for item in checks.values())
    evidence.atomic_json(out, report)
    return 0 if report["verified"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    for name in ("state", "publication", "out"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--installed-app", type=Path, default=Path("/Applications/Skyrocket.app"))
    args = parser.parse_args(argv)
    try:
        return verify(args.root.resolve(), args.state, args.publication, args.out, args.installed_app)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        print("发布后读回失败: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
