#!/usr/bin/env python3
"""Versioned release evidence orchestration for candidate and post-release records."""
from __future__ import annotations

import argparse
import copy
import json
import shlex
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
import sf_constants
import quality_evidence as evidence
import release_state


class EvidenceError(ValueError):
    """A refusal caused by incomplete or mismatched release evidence."""


RECEIPT_RULES = {
    "pre": (
        "./.venv/bin/python3 tools/gate_stamp.py check",
        "./.venv/bin/python3 tools/public_docs_check.py",
    ),
    "bound": (
        "./.venv/bin/python3 tools/smoke_frozen_app.py {app}",
        "./.venv/bin/python3 tools/release_feature_check.py {app}",
        "/usr/bin/codesign --verify --deep --strict {app}",
        "./.venv/bin/python3 tools/verify_candidate_delivery.py --app {app} --zip {zip} --out {delivery} --old-app {old_app}",
    ),
}
REQUIRED_CI_WORKFLOWS = ("ci",)


def version_value(value: str | None) -> str:
    expected = sf_constants.APP_VERSION
    if value is not None and value != expected:
        raise EvidenceError("--version 必须等于 sf_constants.APP_VERSION")
    return expected


def root_path(value: Path | None) -> Path:
    return (value or ROOT).resolve()


def record_path(root: Path, version: str, complete: bool = False) -> Path:
    suffix = "-complete" if complete else ""
    return root / f"quality-v{version}{suffix}.json"


def manifest_path(root: Path, version: str, complete: bool = False) -> Path:
    return record_path(root, version, complete).with_name(
        record_path(root, version, complete).stem + ".manifest.json"
    )


def evidence_dir(root: Path, version: str, complete: bool = False) -> Path:
    suffix = "-complete" if complete else ""
    return root / "build" / f"quality-evidence-v{version}{suffix}"


def load_limitations(path: Path) -> dict[str, list[str]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise EvidenceError(f"limitations 文件无法读取: {exc}") from exc
    if not isinstance(value, dict):
        raise EvidenceError("limitations 文件必须是 JSON 对象")
    result: dict[str, list[str]] = {}
    for key in ("pre", "bound", "complete"):
        items = value.get(key)
        if (not isinstance(items, list) or not items
                or any(not isinstance(item, str) or not item.strip() for item in items)):
            raise EvidenceError(f"limitations.{key} 必须是非空字符串数组")
        result[key] = items
    return result


def command_text(argv: list[str]) -> str:
    return shlex.join(argv)


def receipt_candidates(directory: Path) -> list[tuple[Path, dict[str, Any]]]:
    candidates: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(directory.glob("*/receipt.json")):
        try:
            value = evidence.load(path)
        except (OSError, ValueError, TypeError):
            continue
        candidates.append((path, value))
    return candidates


def pick_receipt(directory: Path, command: str, fingerprint: str) -> tuple[Path, dict[str, Any]]:
    hits: list[tuple[Path, dict[str, Any]]] = []
    for path, value in receipt_candidates(directory):
        argv = value.get("argv")
        if not isinstance(argv, list) or command_text(argv) != command:
            continue
        if value.get("exit_code") == 0 and value.get("stable") is True and value.get("source_start") == fingerprint:
            hits.append((path, value))
    if len(hits) != 1:
        raise EvidenceError(f"命令凭据必须恰好一份: {command}（找到 {len(hits)} 份）")
    return hits[0]


def pick_receipt_contains(directory: Path, needle: str | Path, fingerprint: str,
                          root: Path | None = None) -> tuple[Path, dict[str, Any]]:
    expected_name = Path(needle).name
    expected_path = None
    if root is not None:
        expected_path = (Path(needle) if Path(needle).is_absolute() else root / Path(needle)).resolve()
    hits: list[tuple[Path, dict[str, Any]]] = []
    for path, value in receipt_candidates(directory):
        argv = value.get("argv")
        candidates: list[str] = []
        if isinstance(value.get("output_paths"), list):
            candidates.extend(item for item in value["output_paths"] if isinstance(item, str))
        if isinstance(argv, list):
            candidates.extend(item for item in argv if isinstance(item, str))
        matched = False
        for item in candidates:
            if expected_path is None:
                matched |= Path(item).name == expected_name
            else:
                assert root is not None
                candidate = (Path(item) if Path(item).is_absolute() else root / item).resolve()
                matched |= candidate == expected_path
        if (matched and value.get("exit_code") == 0 and value.get("stable") is True
                and value.get("source_start") == fingerprint):
            hits.append((path, value))
    if len(hits) != 1:
        raise EvidenceError(f"包含目标路径的执行凭据必须恰好一份: {needle}（找到 {len(hits)} 份）")
    return hits[0]


def evidence_item(command: str, receipt: tuple[Path, dict[str, Any]]) -> dict[str, Any]:
    path, value = receipt
    return {"command": command, "exit_code": value["exit_code"], "receipt": str(path.relative_to(path.parents[1]))}


def relative_receipt(path: Path, root: Path) -> str:
    return Path(path).resolve().relative_to(Path(root).resolve()).as_posix()


def relative_path(path: Path, root: Path) -> str:
    """Relativize through real paths because macOS aliases /var to /private/var."""
    return Path(path).resolve().relative_to(Path(root).resolve()).as_posix()


def display_path(path: Path, root: Path) -> str:
    resolved = path.resolve()
    try:
        return relative_path(path, root)
    except ValueError:
        return str(resolved)


def pre_commands(version: str, baseline: str) -> list[str]:
    return [
        RECEIPT_RULES["pre"][0],
        RECEIPT_RULES["pre"][1],
        f"./.venv/bin/python3 tools/diag_coverage_check.py --version {version} --baseline {baseline}",
    ]


def app_paths(root: Path, version: str, app: Path | None, zip_path: Path | None) -> tuple[Path, Path]:
    candidate_app = (app or root / "dist" / "Skyrocket.app").resolve()
    candidate_zip = (zip_path or root / "build" / "update_assets" / f"V{version}" / f"Skyrocket-{version}-macos.zip").resolve()
    return candidate_app, candidate_zip


def expanded_scope(root: Path, names: list[str]) -> list[str]:
    """Declare current tracked/untracked paths so verification cannot hide source changes."""
    try:
        changed = set(filter(None, evidence._git(root, "diff", "HEAD", "--name-only", "-z").split("\0")))
        changed.update(filter(None, evidence._git(root, "ls-files", "--others", "--exclude-standard", "-z").split("\0")))
    except (OSError, ValueError):
        changed = set()
    changed.discard("build/.quality-session.lock")
    return sorted(changed | set(names))


def set_pre_record(root: Path, version: str, baseline: str, limitations: list[str], receipts: list[dict[str, Any]]) -> Path:
    path = record_path(root, version)
    if path.exists():
        raise EvidenceError(f"不覆盖已有质量记录: {path.name}")
    value = evidence.template()
    value["intent"] = f"发布 v{version} 候选；本记录由发布证据工具起草，真实场景保持未验证"
    value["scope"] = expanded_scope(root, [path.name, manifest_path(root, version).name])
    value["acceptance"] = ["回归关卡、公开资料和诊断覆盖均有当前源码同源凭据"]
    value["verification_evidence"] = receipts
    value["acceptance_map"] = [{"acceptance": value["acceptance"][0], "evidence_index": i}
                                for i in range(len(receipts))]
    value["unverified_limitations"] = limitations
    value["artifact_fingerprint"] = {"source": "pending", "artifacts": []}
    value["release_artifacts"]["version"] = version
    value["release_qualification"]["stage"] = "pre_release"
    value["release_qualification"]["baseline"] = baseline
    errors = evidence.validate(value)
    if errors:
        raise EvidenceError("起草记录无效: " + "; ".join(errors))
    evidence.atomic_json(path, value)
    return path


def pre_record(root: Path, version: str, baseline: str, limitations: dict[str, list[str]]) -> Path:
    if not baseline or not baseline.strip():
        raise EvidenceError("pre-record 必须提供 --baseline")
    fingerprint = evidence.source_snapshot(root)["fingerprint"]
    directory = evidence_dir(root, version)
    items = []
    for command in pre_commands(version, baseline):
        receipt = pick_receipt(directory, command, fingerprint)
        items.append({"command": command, "exit_code": 0,
                      "receipt": relative_receipt(receipt[0], root)})
    return set_pre_record(root, version, baseline, limitations["pre"], items)


def artifact_fingerprint(root: Path, version: str, app: Path, zip_path: Path) -> dict[str, Any]:
    dmg = root / "build" / f"Skyrocket-V{version}.dmg"
    ota = root / "build" / "update_assets" / f"V{version}" / "Skyrocket-update.json"
    paths = (app, dmg, zip_path, ota)
    return {"source": evidence.source_snapshot(root)["fingerprint"],
            "artifacts": [{"path": relative_path(path, root)} for path in paths]}


def observation(receipt: dict[str, Any], procedure: str, artifact_sha: str,
                signals: dict[str, bool], attachment: Path, root: Path) -> dict[str, Any]:
    return {"source": receipt["source_start"], "device_id": receipt["machine"]["device_id"],
            "observer": "release_evidence", "procedure": procedure,
            "observed_at": receipt["ended_at"], "artifact_sha256": artifact_sha,
            "signals": signals, "attachments": [{"path": relative_receipt(attachment, root),
                                                    "sha256": evidence.sha256_file(attachment)}]}


def bind(root: Path, version: str, limitations: dict[str, list[str]], old_app: Path,
         app: Path | None = None, zip_path: Path | None = None) -> Path:
    path = record_path(root, version)
    value = evidence.load(path)
    fingerprint = evidence.source_snapshot(root)["fingerprint"]
    if value.get("artifact_fingerprint", {}).get("source") not in ("pending", fingerprint):
        raise EvidenceError("候选记录源码指纹已变化")
    first = [pick_receipt(evidence_dir(root, version), RECEIPT_RULES["pre"][0], fingerprint),
             pick_receipt(evidence_dir(root, version), RECEIPT_RULES["pre"][1], fingerprint)]
    diag_command = pre_commands(version, str(value.get("release_qualification", {}).get("baseline", "")))[2]
    diag_hits = [item for item in receipt_candidates(evidence_dir(root, version))
                 if isinstance(item[1].get("argv"), list)
                 and command_text(item[1]["argv"]) == diag_command
                 and item[1].get("exit_code") == 0 and item[1].get("stable") is True
                 and item[1].get("source_start") == fingerprint]
    if len(diag_hits) != 1:
        raise EvidenceError(f"诊断覆盖凭据必须恰好一份且同源（找到 {len(diag_hits)} 份）")
    candidate_app, candidate_zip = app_paths(root, version, app, zip_path)
    delivery = evidence_dir(root, version) / "delivery.json"
    old_display = display_path(old_app, root)
    bound_commands = [
        RECEIPT_RULES["bound"][0].format(app=relative_path(candidate_app, root)),
        RECEIPT_RULES["bound"][1].format(app=relative_path(candidate_app, root)),
        RECEIPT_RULES["bound"][2].format(app=relative_path(candidate_app, root)),
        RECEIPT_RULES["bound"][3].format(app=relative_path(candidate_app, root),
                                          zip=relative_path(candidate_zip, root),
                                          delivery=delivery.relative_to(root).as_posix(),
                                          old_app=old_display),
    ]
    chosen = first + [diag_hits[0]]
    for command in bound_commands:
        chosen.append(pick_receipt(evidence_dir(root, version), command, fingerprint))
    pre_receipts = [{"command": command_text(item[1]["argv"]), "exit_code": 0,
                     "receipt": relative_receipt(item[0], root)} for item in chosen]
    value["status"] = "release_candidate"
    value["release_qualification"]["stage"] = "pre_release"
    value["release_artifacts"] = {"version": version, "roles": {
        "app": {"path": relative_path(candidate_app, root)},
        "dmg": {"path": relative_path(root / "build" / f"Skyrocket-V{version}.dmg", root)},
        "zip": {"path": relative_path(candidate_zip, root)},
        "ota_manifest": {"path": relative_path(root / "build" / "update_assets" / f"V{version}" / "Skyrocket-update.json", root)}},
        "ota": {"archive_role": "zip", "manifest_role": "ota_manifest"}}
    value["artifact_fingerprint"] = artifact_fingerprint(root, version, candidate_app, candidate_zip)
    value["verification_evidence"] = pre_receipts
    value["acceptance"] = ["回归关卡、公开资料和诊断覆盖均有当前源码同源凭据",
                            "候选冻结包、特征、签名和候选交付核验均有当前源码同源凭据"]
    value["acceptance_map"] = [{"acceptance": value["acceptance"][0], "evidence_index": i} for i in range(3)]
    value["acceptance_map"] += [{"acceptance": value["acceptance"][1], "evidence_index": i} for i in range(3, 7)]
    value["unverified_limitations"] = limitations["bound"]
    app_sha = evidence.artifact_token(candidate_app)["sha256"]
    smoke_command = str(pre_receipts[3]["command"])
    delivery_command = str(pre_receipts[6]["command"])
    smoke_path, smoke = pick_receipt(evidence_dir(root, version), smoke_command, fingerprint)
    delivery_path, delivery_receipt = pick_receipt(evidence_dir(root, version), delivery_command, fingerprint)
    for item in value["scenarios"]:
        if item["id"] == "local_mac":
            item.update(status="passed", receipts=[relative_receipt(smoke_path, root)],
                        reason="候选冻结包冒烟证据", observation=observation(smoke, smoke_command, app_sha,
                        {"first_frame": True, "clean_exit": True}, smoke_path.parent / smoke.get("log", "command.log"), root))
        elif item["id"] in {"fresh_install", "upgrade_install", "ota_rollback"}:
            item.update(status="passed", receipts=[relative_receipt(delivery_path, root)],
                        reason="候选交付隔离验收证据", observation=observation(delivery_receipt, delivery_command, app_sha,
                        {"installed_version": True, "signature_valid": True, "first_frame": True}
                        if item["id"] == "fresh_install" else
                        {"installed_version": True, "user_data_preserved": True, "first_frame": True}
                        if item["id"] == "upgrade_install" else
                        {"failure_injected": True, "rollback_restored": True, "old_app_started": True,
                         "user_data_preserved": True}, delivery_path.parent / delivery_receipt.get("log", "command.log"), root))
    errors = evidence.validate(value)
    if errors:
        raise EvidenceError("绑定记录无效: " + "; ".join(errors))
    evidence.atomic_json(path, value)
    write_qualification(root, value, path)
    return path


def write_qualification(root: Path, value: dict[str, Any], path: Path) -> None:
    manifest = evidence.manifest(root, value, path.parent)
    manifest_file = path.with_suffix(".manifest.json")
    evidence.atomic_json(manifest_file, manifest)
    verify_errors = evidence.verify_release_candidate(root, value, path.parent, manifest_file)
    out_dir = evidence_dir(root, value["release_artifacts"]["version"])
    evidence.atomic_json(out_dir / "verify.json", {"errors": verify_errors})
    evidence.atomic_json(out_dir / "qualify.json", evidence.qualification_report(root, value, path.parent, manifest_file))
    if verify_errors:
        raise EvidenceError("正式候选资格核验失败: " + "; ".join(verify_errors))


def complete(root: Path, version: str, limitations: dict[str, list[str]], state: Path,
             readback: Path, walkthrough: Path, evidence_path: Path | None = None,
             label: str | None = None) -> Path:
    folder = evidence.release_identity.archive_folder_name(version, label)  # 标签不合法时动手之前就拒绝
    pre_path = record_path(root, version)
    value = evidence.load(pre_path)
    fingerprint = evidence.source_snapshot(root)["fingerprint"]
    if value.get("status") != "release_candidate":
        raise EvidenceError("发布后记录只能从 release_candidate 候选记录生成")
    if value.get("artifact_fingerprint", {}).get("source") != fingerprint:
        raise EvidenceError("发布后记录候选源码指纹已变化")
    directory = evidence_path or evidence_dir(root, version, True)
    read_receipt = pick_receipt_contains(directory, readback.name, fingerprint, root)
    walk_receipt = pick_receipt_contains(directory, walkthrough.name, fingerprint, root)
    value = copy.deepcopy(value)
    value["change_id"] = "CHANGE-" + uuid.uuid4().hex[:12]
    value["release_qualification"]["stage"] = "complete"
    value["intent"] += "（发布后独立读回与装机版走查）"
    value["scope"] = [pre_path.name, manifest_path(root, version).name,
                      record_path(root, version, True).name, manifest_path(root, version, True).name,
                      f"outputs/release-candidates/{folder}/online-manifest.json",
                      f"outputs/release-candidates/{folder}/post_release.json",
                      f"outputs/release-candidates/{folder}/publish.log"]
    value["unverified_limitations"] = limitations["complete"]
    value["verification_evidence"] += [
        {"command": command_text(read_receipt[1]["argv"]), "exit_code": 0,
         "receipt": relative_receipt(read_receipt[0], root)},
        {"command": command_text(walk_receipt[1]["argv"]), "exit_code": 0,
         "receipt": relative_receipt(walk_receipt[0], root)},
    ]
    value["acceptance"] = list(value["acceptance"]) + ["发布后独立读回报告有同源执行凭据",
                                                           "装机版走查结果有同源执行凭据"]
    start = len(value["verification_evidence"]) - 2
    value["acceptance_map"] += [{"acceptance": value["acceptance"][-2], "evidence_index": start},
                                 {"acceptance": value["acceptance"][-1], "evidence_index": start + 1}]
    out = record_path(root, version, True)
    if out.exists():
        raise EvidenceError(f"不覆盖已有发布后记录: {out.name}")
    evidence.atomic_json(out, value)
    write_qualification(root, value, out)
    release_state.finish(root, state, out, readback, walkthrough)
    return out


def gh_runs(repo: str, sha: str) -> list[dict[str, Any]]:
    result = subprocess.run(["gh", "run", "list", "--repo", repo, "--commit", sha,
                             "--limit", "20", "--json", "databaseId,status,conclusion,headSha,workflowName"],
                            capture_output=True, text=True, check=False)
    if result.returncode:
        raise EvidenceError("gh run 查询失败: " + result.stderr.strip())
    try:
        value = json.loads(result.stdout or "[]")
    except (TypeError, ValueError) as exc:
        raise EvidenceError("gh run 返回格式无效") from exc
    if not isinstance(value, list):
        raise EvidenceError("gh run 返回格式无效")
    return [item for item in value if isinstance(item, dict) and item.get("headSha") in (None, sha)]


def ci_workflow_name(run: dict[str, Any]) -> str:
    return str(run.get("workflowName", run.get("name", ""))).strip().lower()


def wait_ci(repo: str, sha: str, timeout: float = 600.0, interval: float = 5.0,
            query: Callable[[str, str], list[dict[str, Any]]] | None = None,
            clock: Callable[[], float] = time.monotonic,
            sleeper: Callable[[float], None] = time.sleep) -> int:
    query = query or gh_runs
    deadline = clock() + timeout
    while clock() <= deadline:
        runs = query(repo, sha)
        selected = {ci_workflow_name(run): run for run in runs if ci_workflow_name(run) in REQUIRED_CI_WORKFLOWS}
        if len(selected) == len(REQUIRED_CI_WORKFLOWS):
            if any(run.get("status") in {"queued", "in_progress"} for run in selected.values()):
                pass
            elif all(run.get("status") == "completed" and run.get("conclusion") == "success"
                     for run in selected.values()):
                ids = ",".join(str(run.get("databaseId", run.get("id", "unknown"))) for run in selected.values())
                print(f"CI success run_id={ids}")
                return 0
            elif any(run.get("status") == "completed" and run.get("conclusion") in
                     {"failure", "cancelled", "timed_out"} for run in selected.values()):
                print("CI failure", file=sys.stderr)
                return 1
        remaining = deadline - clock()
        if remaining <= 0:
            break
        sleeper(min(interval, remaining))
    print("CI wait timeout", file=sys.stderr)
    return 3


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("command", choices=("pre-record", "bind", "complete", "wait-ci"))
    value.add_argument("--version")
    value.add_argument("--root", type=Path, default=ROOT)
    value.add_argument("--limitations-file", type=Path)
    value.add_argument("--baseline")
    value.add_argument("--old-app", type=Path)
    value.add_argument("--app", type=Path)
    value.add_argument("--zip", dest="zip_path", type=Path)
    value.add_argument("--state", type=Path)
    value.add_argument("--readback", type=Path)
    value.add_argument("--walkthrough", type=Path)
    value.add_argument("--evidence-dir", type=Path)
    value.add_argument("--label", help="同版本号再发布时的归档标签，范围里的归档文件夹随之变为 vX.Y.Z-标签")
    value.add_argument("--repo")
    value.add_argument("--sha")
    value.add_argument("--timeout", type=float, default=600.0)
    value.add_argument("--interval", type=float, default=5.0)
    return value


def main(argv: list[str] | None = None) -> int:
    raw = sys.argv[1:] if argv is None else argv
    if raw == ["--self-test"]:
        return self_test()
    if "--self-test" in raw:
        print("--self-test 不能与其它参数混用", file=sys.stderr)
        return 2
    try:
        args = parser().parse_args(raw)
        version = version_value(args.version)
        root = root_path(args.root)
        if args.command == "wait-ci":
            if not args.repo or not args.sha or args.timeout <= 0 or args.interval < 0:
                raise EvidenceError("wait-ci 必须提供有效 --repo、--sha、--timeout 和 --interval")
            return wait_ci(args.repo, args.sha, args.timeout, args.interval)
        if not args.limitations_file:
            raise EvidenceError("必须提供 --limitations-file")
        limitations = load_limitations(args.limitations_file)
        if args.command == "pre-record":
            return 0 if pre_record(root, version, args.baseline or "", limitations) else 1
        if args.command == "bind":
            if not args.old_app:
                raise EvidenceError("bind 必须提供 --old-app")
            return 0 if bind(root, version, limitations, args.old_app, args.app, args.zip_path) else 1
        required = (args.state, args.readback, args.walkthrough)
        if any(item is None for item in required):
            raise EvidenceError("complete 必须提供 --state、--readback、--walkthrough")
        complete(root, version, limitations, args.state, args.readback, args.walkthrough, args.evidence_dir,
                 args.label)
        return 0
    except (EvidenceError, OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(f"release_evidence: {exc}", file=sys.stderr)
        return 2 if isinstance(exc, (EvidenceError, ValueError, KeyError, TypeError, json.JSONDecodeError)) else 1


def self_test() -> int:
    checks = 0
    for bad in ({"pre": ["ok"], "bound": [], "complete": ["ok"]},
                {"pre": ["ok"], "bound": [""], "complete": ["ok"]}):
        try:
            path = ROOT / "build" / ".release-evidence-self-test.json"
            path.parent.mkdir(exist_ok=True)
            path.write_text(json.dumps(bad), encoding="utf-8")
            load_limitations(path)
        except EvidenceError:
            checks += 1
        finally:
            path.unlink(missing_ok=True)
    if wait_ci("fixture/repo", "sha", timeout=0, query=lambda *_: [], clock=lambda: 1.0, sleeper=lambda _: None) != 3:
        return 1
    print(f"release_evidence self-test passed ({checks + 1} checks)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
