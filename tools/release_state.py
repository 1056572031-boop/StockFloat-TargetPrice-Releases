#!/usr/bin/env python3
"""发布阶段索引与总报告；复用正式质量政策，不替代真实场景取证。"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
import gate_stamp
import public_docs_check
import quality_evidence as evidence

STAGES = ("source_open", "source_qualified", "candidate_ready",
          "released_pending_acceptance", "complete")
DOCS = ("README.md", "docs/INSTALL.md", "release_notes.txt", "SECURITY.md", "NOTICE.md", "LICENSE")


def source_snapshot(root: Path) -> dict[str, Any]:
    info = gate_stamp.current_info(str(root))
    return {"head": info["head"], "gate_fingerprint": info["fingerprint"],
            "gate_input_count": info["files"], "workspace_token": evidence.workspace_token(root),
            "docs": {name: evidence.sha256_file(root / name) for name in DOCS}}


def reference(path: Path) -> dict[str, Any]:
    return {"path": str(path.resolve()), **evidence.artifact_token(path)}


def init_state(root: Path, path: Path, version: str) -> dict[str, Any]:
    if path.exists():
        raise ValueError("不覆盖已有发布状态；先保留旧记录，再用新 --state 路径开始本轮")
    state = {"schema_version": 1, "version": version, "stage": "source_open",
             "updated_at": evidence.now(), "source": source_snapshot(root), "artifacts": {},
             "history": [], "observations": {}}
    evidence.atomic_json(path, state)
    return state


def validate(root: Path, path: Path, expected_stage: str | None = None) -> dict[str, Any]:
    state = evidence.load(path)
    if state.get("schema_version") != 1 or state.get("stage") not in STAGES:
        raise ValueError("发布状态结构无效")
    if state.get("source") != source_snapshot(root):
        raise ValueError("发布状态已失效：源码或文档已变化，必须重新回归并生成候选")
    if expected_stage and state["stage"] != expected_stage:
        raise ValueError(f"发布状态不是 {expected_stage}: {state['stage']}")
    for group in ("artifacts", "observations"):
        for item in state[group].values():
            if reference(Path(item["path"])) != item:
                raise ValueError("已绑定文件发生变化: " + item["path"])
    return state


def advance(path: Path, state: dict[str, Any], stage: str) -> dict[str, Any]:
    state["history"].append({"from": state["stage"], "to": stage, "at": evidence.now()})
    state.update(stage=stage, updated_at=evidence.now())
    evidence.atomic_json(path, state)
    return state


def check_coverage(root: Path) -> None:
    subprocess.run([str(root / ".venv/bin/python3"), "tools/coverage_provenance.py", "check"],
                   cwd=root, env=gate_stamp.git_env(), check=True, capture_output=True, text=True)


def qualify_source(root: Path, path: Path) -> dict[str, Any]:
    state = validate(root, path, "source_open")
    valid, reason = gate_stamp.check(str(root))
    if not valid:
        raise ValueError("完整回归关卡不可用: " + reason)
    check_coverage(root)
    problems = public_docs_check.problems(root, state["version"])
    if problems:
        raise ValueError("公开资料未同源: " + "; ".join(problems))
    if state["source"] != source_snapshot(root):
        raise ValueError("检查期间源码已变化")
    return advance(path, state, "source_qualified")


def make_candidate(root: Path, path: Path, version: str, app: Path, dmg: Path,
                   archive: Path, ota: Path) -> dict[str, Any]:
    state = validate(root, path, "source_qualified")
    if state["version"] != version:
        raise ValueError("状态版本与当前候选版本不一致")
    state["artifacts"] = {role: reference(value) for role, value in
                          (("app", app), ("dmg", dmg), ("zip", archive), ("ota_manifest", ota))}
    expected_kinds = {"app": "directory", "dmg": "file", "zip": "file", "ota_manifest": "file"}
    if any(state["artifacts"][role]["kind"] != kind for role, kind in expected_kinds.items()):
        raise ValueError("候选资产类型错误")
    manifest = evidence.load(ota)
    binding = manifest.get("archive", {})
    if (manifest.get("version") != version or binding.get("sha256") != evidence.sha256_file(archive)
            or binding.get("size") != archive.stat().st_size):
        raise ValueError("OTA 清单未绑定本版 ZIP")
    return advance(path, state, "candidate_ready")


def quality(root: Path, state: dict[str, Any], record_path: Path, complete: bool = False) -> dict[str, Any]:
    record = evidence.load(record_path)
    errors = list(evidence.verify_release_candidate(root, record, record_path.parent,
                                                    record_path.with_suffix(".manifest.json")))
    if record.get("release_artifacts", {}).get("version") != state["version"]:
        errors.append("质量记录版本不匹配")
    for role, item in state["artifacts"].items():
        value = record.get("release_artifacts", {}).get("roles", {}).get(role, {})
        declared = evidence.resolve_reference(record_path.parent, value.get("path", ""))
        if declared.resolve() != Path(item["path"]):
            errors.append("质量记录未绑定状态中的候选路径: " + role)
    if complete and record.get("release_qualification", {}).get("stage") != "complete":
        errors.append("质量记录尚未完成真实验收")
    if errors:
        raise ValueError("; ".join(errors))
    return record


def begin_publish(root: Path, path: Path, record_path: Path) -> dict[str, Any]:
    state = validate(root, path, "candidate_ready")
    quality(root, state, record_path)
    state["observations"]["publish_quality"] = reference(record_path)
    state["observations"]["publish_manifest"] = reference(record_path.with_suffix(".manifest.json"))
    # Mark pending before the first irreversible write; interruption is never complete.
    return advance(path, state, "released_pending_acceptance")


def check_walkthrough(app: dict[str, Any], evidence_path: Path) -> dict[str, Any]:
    """Require a real installed-app walkthrough result before marking a release complete."""
    evidence_value = evidence.load(evidence_path)
    walked_app = Path(evidence_value.get("app", ""))
    if (not walked_app.is_dir()
            or evidence.artifact_token(walked_app) != {key: value for key, value in app.items() if key != "path"}):
        raise ValueError("装机点击走查不是当前候选包")
    if evidence_value.get("version") != _bundle_version(walked_app):
        raise ValueError("装机点击走查版本与安装包不一致")
    summary = evidence_value.get("summary", {})
    if (not isinstance(summary, dict) or summary.get("ok") is not True
            or summary.get("ran") != 6 or summary.get("passed") != 6
            or summary.get("failed") != 0 or summary.get("skipped") != 0):
        raise ValueError("装机点击走查未完整通过")
    user_data = evidence_value.get("user_data", {})
    if not isinstance(user_data, dict) or user_data.get("ok") is not True:
        raise ValueError("装机点击走查没有保持用户数据隔离")
    return reference(evidence_path)


def _bundle_version(app: Path) -> str:
    import plistlib

    try:
        with (app / "Contents" / "Info.plist").open("rb") as stream:
            value = plistlib.load(stream).get("CFBundleShortVersionString")
    except (OSError, plistlib.InvalidFileException, ValueError) as exc:
        raise ValueError("安装包版本不可读") from exc
    if not isinstance(value, str) or not value:
        raise ValueError("安装包版本不可读")
    return value


def finish(root: Path, path: Path, record_path: Path, readback: Path,
           walkthrough: Path | None = None) -> dict[str, Any]:
    state = validate(root, path, "released_pending_acceptance")
    quality(root, state, record_path, complete=True)
    report = evidence.load(readback)
    if (report.get("verified") is not True or report.get("version") != state["version"]
            or report.get("artifacts") != state["artifacts"]
            or report.get("source") != state["source"]):
        raise ValueError("缺少同源发布后独立读回")
    required = ("ota_manifest", "zip", "dmg", "public_source", "checksums", "public_ci", "final_startup")
    if any(report.get("checks", {}).get(name, {}).get("status") != "passed" for name in required):
        raise ValueError("发布后渠道或最终启动未全部通过")
    try:
        observed_at = dt.datetime.fromisoformat(report.get("generated_at", ""))
        publish_at = dt.datetime.fromisoformat(state["updated_at"])
        if observed_at.tzinfo is None or publish_at.tzinfo is None:
            raise ValueError("读回时间必须带时区")
    except (TypeError, ValueError) as exc:
        raise ValueError("发布后读回时间无效") from exc
    if observed_at < publish_at or observed_at > evidence.utc_now():
        raise ValueError("读回不是本轮发布阶段之后的实际观察")
    publication = report.get("publication", {})
    downloads = report.get("downloaded_files", {})
    if not publication or not downloads:
        raise ValueError("发布后读回缺少实际下载文件或公开发布回执")
    if reference(Path(publication["path"])) != publication:
        raise ValueError("公开发布回执已变化")
    public = evidence.load(Path(publication["path"]))
    commit = public.get("public_commit")
    if (public.get("version") != state["version"] or public.get("source") != state["source"]
            or not isinstance(commit, str) or not commit
            or report["checks"]["public_source"].get("observation", {}).get("commit") != commit
            or report["checks"]["public_ci"].get("observation", {}).get("head_sha") != commit):
        raise ValueError("公开发布回执、源码与 CI 未绑定同一版本和提交")
    for role in ("source_zip", "checksums"):
        item = public.get(role)
        if (not isinstance(item, dict) or not isinstance(item.get("path"), str)
                or not item.get("path") or not item.get("content_sha256")):
            raise ValueError("公开发布回执缺少源码或校验文件身份")
    expected = {
        Path(state["artifacts"]["zip"]["path"]).name: state["artifacts"]["zip"]["content_sha256"],
        Path(state["artifacts"]["dmg"]["path"]).name: state["artifacts"]["dmg"]["content_sha256"],
        "online-manifest.json": state["artifacts"]["ota_manifest"]["content_sha256"],
        Path(public["source_zip"]["path"]).name: public["source_zip"]["content_sha256"],
        Path(public["checksums"]["path"]).name: public["checksums"]["content_sha256"],
    }
    if len(expected) != 5 or not set(expected).issubset(downloads):
        raise ValueError("发布后完整下载证据缺失：应用 ZIP、DMG、OTA 清单、源码 ZIP、校验文件必须齐全")
    for item in downloads.values():
        if reference(Path(item["path"])) != item:
            raise ValueError("发布后下载证据已变化")
    if any(downloads[name].get("content_sha256") != digest for name, digest in expected.items()):
        raise ValueError("实际下载内容与已绑定发布资产不一致")
    if walkthrough is None:
        raise ValueError("缺少装机点击走查证据")
    walkthrough_reference = check_walkthrough(state["artifacts"]["app"], walkthrough)
    state["observations"]["publication"] = publication
    state["observations"].update({"download_" + name: item for name, item in downloads.items()})
    state["observations"].update(acceptance_quality=reference(record_path),
                                 acceptance_manifest=reference(record_path.with_suffix(".manifest.json")),
                                 readback=reference(readback),
                                 click_walkthrough=walkthrough_reference)
    return advance(path, state, "complete")


def report(root: Path, path: Path, record_path: Path | None = None,
           readback: Path | None = None, delivery: Path | None = None) -> dict[str, Any]:
    state = evidence.load(path)
    blockers = []
    current = True
    try:
        validate(root, path)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        blockers.append(str(exc))
        current = False
    if record_path is None:
        bound = state.get("observations", {})
        item = bound.get("acceptance_quality", bound.get("publish_quality"))
        record_path = Path(item["path"]) if item else None
    try:
        record = evidence.load(record_path) if record_path else {}
    except (OSError, ValueError) as exc:
        blockers.append("质量记录无法读取: " + str(exc))
        record = {}
    if record_path and state.get("artifacts"):
        try:
            quality(root, state, record_path, complete=state["stage"] == "complete")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            blockers.append(str(exc))
    scenarios = {item["id"]: item for item in record.get("scenarios", [])}
    matrix = [scenarios.get(item["id"], item) for item in evidence.template()["scenarios"]]
    channel_names = ("ota_manifest", "zip", "dmg", "public_source", "checksums", "public_ci", "final_startup")
    channels = {name: {"status": "unverified"} for name in channel_names}
    delivery_matrix = {name: {"status": "unverified"} for name in
                       ("candidate_startup", "fresh_install", "upgrade_install", "ota_forward",
                        "ota_rollback", "user_data_readback", "old_app_restart")}
    if readback is None and "readback" in state["observations"]:
        readback = Path(state["observations"]["readback"]["path"])
    for kind, input_path in (("readback", readback), ("delivery", delivery)):
        if input_path is None:
            continue
        try:
            value = evidence.load(input_path)
            if kind == "readback":
                if value.get("source") != state["source"] or value.get("artifacts") != state["artifacts"]:
                    raise ValueError("发布后读回与状态不同源")
                channels.update(value.get("checks", {}))
            else:
                for role in ("app", "zip"):
                    expected = {key: data for key, data in state["artifacts"][role].items() if key != "path"}
                    if value.get("artifacts", {}).get(role) != expected:
                        raise ValueError("候选交付报告与资产不同源: " + role)
                delivery_matrix.update(value.get("matrix", {}))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            blockers.append(str(exc))
    return {"schema_version": 1, "generated_at": evidence.now(), "version": state["version"],
            "recorded_stage": state["stage"], "effective_stage": state["stage"] if current else "source_open",
            "complete": state["stage"] == "complete" and not blockers,
            "source": state["source"], "artifacts": state["artifacts"],
            "observations": state["observations"], "matrix": matrix, "blockers": blockers,
            "channels": channels, "candidate_delivery": delivery_matrix,
            "pending": [item["id"] for item in matrix if item["id"] in evidence.RELEASE_REQUIRED_SCENARIOS
                        and item["status"] != "passed"],
            "boundary": "候选检查不等于已发布；四专项不适用和 Intel 不支持均不等于通过；未验证不进入 complete"}


def write_report(root: Path, path: Path, output: Path, record_path: Path | None = None,
                 readback: Path | None = None, delivery: Path | None = None) -> dict[str, Any]:
    value = report(root, path, record_path, readback, delivery)
    evidence.atomic_json(output, value)
    lines = ["# 发布总报告", "", "版本：" + value["version"], "阶段：" + value["effective_stage"],
             "发布验收完成：" + ("是" if value["complete"] else "否"), "", value["boundary"], "", "## 阻断项"]
    lines += ["- " + item for item in value["blockers"]] or ["- 未发现阻断；不代表未执行项目通过"]
    lines += ["", "## 场景矩阵", "", "| 项目 | 状态 | 说明 |", "| --- | --- | --- |"]
    lines += [f"| {item['id']} | {item['status']} | {item.get('reason', '')} |" for item in value["matrix"]]
    for title, key in (("候选交付（不等于发布后验收）", "candidate_delivery"), ("发布后渠道读回", "channels")):
        lines += ["", "## " + title, "", "| 项目 | 状态 |", "| --- | --- |"]
        lines += [f"| {name} | {item['status']} |" for name, item in value[key].items()]
    lines += ["", "## 绑定证据", "", "```json", json.dumps({key: value[key] for key in
              ("source", "artifacts", "observations")}, ensure_ascii=False, indent=2), "```", ""]
    output.with_suffix(".md").write_text("\n".join(lines), encoding="utf-8")
    return value


def self_test() -> int:
    """真实临时仓库的拒绝样本；不伪造完整关卡、不触碰安装版或远端。"""
    with tempfile.TemporaryDirectory(prefix="sf_release_state_selftest_") as temp:
        root = Path(temp).resolve()
        env = gate_stamp.git_env()
        env.update(GIT_AUTHOR_NAME="Fixture", GIT_AUTHOR_EMAIL="fixture@example.test",
                   GIT_COMMITTER_NAME="Fixture", GIT_COMMITTER_EMAIL="fixture@example.test")
        for name in DOCS:
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("fixture", encoding="utf-8")
        (root / "x.py").write_text("value = 1\n", encoding="utf-8")
        (root / ".gitignore").write_text("build/\n", encoding="utf-8")
        for command in (["init", "-q"], ["add", "."], ["commit", "-qm", "fixture"]):
            subprocess.run(["git", *command], cwd=root, env=env, check=True, capture_output=True)
        path = root / "build/state.json"
        init_state(root, path, "fixture-version")
        validate(root, path, "source_open")
        rejected = 0
        try:
            begin_publish(root, path, root / "missing-quality.json")
        except ValueError:
            rejected += 1
        (root / "x.py").write_text("value = 2\n", encoding="utf-8")
        try:
            validate(root, path)
        except ValueError:
            rejected += 1
        (root / "x.py").write_text("value = 1\n", encoding="utf-8")
        app = root / "build/Skyrocket.app"
        app.mkdir()
        payload = app / "payload"
        payload.write_bytes(b"original")
        state = evidence.load(path)
        state["artifacts"] = {"app": reference(app)}
        evidence.atomic_json(path, state)
        payload.write_bytes(b"changed")
        try:
            validate(root, path)
        except ValueError:
            rejected += 1
        if rejected != 3:
            print("release_state 自检失败：应拒绝三种错误，实际拒绝 " + str(rejected))
            return 1
    print("release_state 自检通过：跳阶段、源码变化、APP 目录篡改均拒绝；不等于真实发布通过")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    raw = sys.argv[1:] if argv is None else argv
    if "--self-test" in raw:
        if raw != ["--self-test"]:
            parser.error("--self-test 不可与发布状态操作混用")
        return self_test()
    parser.add_argument("command", choices=("init", "qualify-source", "candidate", "begin-publish", "complete", "validate", "report"))
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--state", type=Path, required=True)
    for name in ("app", "dmg", "zip", "ota", "quality", "readback", "walkthrough", "delivery", "out"):
        parser.add_argument("--" + name, type=Path)
    parser.add_argument("--version")
    parser.add_argument("--stage", choices=STAGES)
    args = parser.parse_args(raw)
    required = {"init": ("version",), "candidate": ("version", "app", "dmg", "zip", "ota"),
                "begin-publish": ("quality",), "complete": ("quality", "readback", "walkthrough"), "report": ("out",)}
    if any(getattr(args, name) is None for name in required.get(args.command, ())):
        parser.error("缺少本阶段必需参数: " + ", ".join(required[args.command]))
    try:
        root = args.root.resolve()
        if args.command == "init":
            value = init_state(root, args.state, args.version)
        elif args.command == "qualify-source":
            value = qualify_source(root, args.state)
        elif args.command == "candidate":
            value = make_candidate(root, args.state, args.version, args.app, args.dmg, args.zip, args.ota)
        elif args.command == "begin-publish":
            value = begin_publish(root, args.state, args.quality)
        elif args.command == "complete":
            value = finish(root, args.state, args.quality, args.readback, args.walkthrough)
        elif args.command == "report":
            value = write_report(root, args.state, args.out, args.quality, args.readback, args.delivery)
        else:
            value = validate(root, args.state, args.stage)
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as exc:
        print(f"release_state: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
