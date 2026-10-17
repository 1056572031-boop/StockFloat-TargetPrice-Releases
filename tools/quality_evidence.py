#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""质量证据链。init 起草；run 真实执行并留据；manifest 绑定产物；verify 只核验。

本地证据用于发现遗漏和过期，不是对恶意本机管理员的密码学证明。
未验证的真机场景不转成通过；只有全部必需场景闭环才可标记 complete。
"""
from __future__ import annotations

import argparse
import ast
import datetime as dt
import fcntl
import hashlib
import importlib.util
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
_gate_spec = importlib.util.spec_from_file_location("gate_stamp", ROOT / "tools" / "gate_stamp.py")
if _gate_spec is None or _gate_spec.loader is None:
    raise ImportError("无法加载 gate_stamp")
gate_stamp = importlib.util.module_from_spec(_gate_spec)
_gate_spec.loader.exec_module(gate_stamp)

REQUIRED = ("change_id", "intent", "scope", "acceptance", "acceptance_map",
            "verification_evidence", "artifact_fingerprint", "unverified_limitations")
SCHEMA_VERSION = 1
SCENARIOS = ("local_mac", "second_mac", "intel", "fresh_install", "upgrade_install",
             "camera_upgrade", "fullscreen", "browser_download", "ota_rollback")
RELEASE_POLICY_VERSION = 2
RELEASE_ARTIFACT_ROLES = ("app", "dmg", "zip", "ota_manifest")
# This is a closed policy list.  A record cannot add an exemption or turn off a
# required scenario; the four entries below are the only permanently excluded
#专项, and their exclusion is not evidence of success.
PERMANENT_EXEMPTIONS = {
    "fullscreen": "用户永久排除第三方浏览器专项；不代表常规自动全屏测试通过",
    "camera_upgrade": "用户永久排除 camera_upgrade 专项；不代表摄像头升级验证通过",
    "second_mac": "用户永久排除 second_mac 专项；不代表第二台 Mac 实测通过",
    "browser_download": "用户永久排除 browser_download/Gatekeeper 专项；不代表下载与 Gatekeeper 实测通过",
}
CAPABILITY_BOUNDARIES = {
    "intel": {
        "status": "not_applicable",
        "required": False,
        "reason": "当前产品不支持 ARM 包安装到 Intel；不支持不代表通过，也不得声称 Intel 实测通过",
    },
}
# The policy is intentionally independent from record[].required. A change record
# may describe a development fixture, but it cannot disable release obligations.
RELEASE_SCENARIO_PHASES = {
    "local_mac": "pre_release",
    "fresh_install": "pre_release",
    "upgrade_install": "pre_release",
    "fullscreen": "pre_release",
    "second_mac": "post_release",
    "intel": "post_release",
    "camera_upgrade": "post_release",
    "browser_download": "post_release",
    "ota_rollback": "post_release",
}
RELEASE_PRE_SCENARIOS = tuple(name for name in SCENARIOS
                              if RELEASE_SCENARIO_PHASES[name] == "pre_release"
                              and name not in PERMANENT_EXEMPTIONS
                              and name not in CAPABILITY_BOUNDARIES)
RELEASE_POST_SCENARIOS = tuple(name for name in SCENARIOS
                               if RELEASE_SCENARIO_PHASES[name] == "post_release"
                               and name not in PERMANENT_EXEMPTIONS
                              and name not in CAPABILITY_BOUNDARIES)
RELEASE_REQUIRED_SCENARIOS = tuple(name for name in SCENARIOS
                                   if name not in PERMANENT_EXEMPTIONS
                                   and name not in CAPABILITY_BOUNDARIES)
LEVELS = {1: "纯函数和规则", 2: "真实组件与隔离协作者", 3: "冻结产物与系统集成", 4: "用户设备和交付通路"}
TEST_MATRIX = {
    "release_chain": {"levels": [1, 2, 3], "checks": ["full_regression", "mutation", "artifact_identity"]},
    "data_compatibility": {"levels": [1, 2], "checks": ["legacy_read", "roundtrip", "failed_write"]},
    "native_ui": {"levels": [2, 3, 4], "checks": ["intermediate_state", "failure_visibility", "fullscreen"]},
}
SCENARIO_SIGNALS = {
    "local_mac": ("first_frame", "clean_exit"),
    "second_mac": ("first_frame", "clean_exit"),
    "intel": ("first_frame", "clean_exit", "native_x86_64"),
    "fresh_install": ("installed_version", "signature_valid", "first_frame"),
    "upgrade_install": ("installed_version", "user_data_preserved", "first_frame"),
    "camera_upgrade": ("baseline_authorized", "upgrade_authorized", "restart_authorized"),
    "fullscreen": ("menu_onscreen", "editor_onscreen", "target_bubble_onscreen"),
    "browser_download": ("browser_downloaded", "finder_opened_dmg", "installed_first_frame"),
    "ota_rollback": ("failure_injected", "rollback_restored", "old_app_started", "user_data_preserved"),
}


def _git_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, env=_git_env(),
                            capture_output=True, text=True, check=False)
    if result.returncode:
        raise ValueError(f"Git 无法读取 {args[0]}: {result.stderr.strip()}")
    return result.stdout


def input_paths(root: Path) -> list[Path]:
    _git(root, "rev-parse", "--show-toplevel")
    return [root / p for p in gate_stamp.build_input_paths(str(root))]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def source_snapshot(root: Path) -> dict[str, Any]:
    files = input_paths(root)
    if not files:
        raise ValueError("无法取得构建输入：输入清单为空")
    entries = []
    for path in files:
        path.lstat()
        token = gate_stamp.file_token(str(path))
        if token in ("missing", "unreadable", "link:?"):
            raise ValueError(f"无法读取构建输入: {path.relative_to(root)}")
        entries.append({"path": path.relative_to(root).as_posix(), "token": token})
    # 与回归戳完全相同的 token、顺序和聚合算法，不另造第二种源码身份。
    digest = hashlib.sha256()
    for entry in entries:
        digest.update(entry["path"].encode("utf-8", "surrogateescape"))
        digest.update(b"\0")
        digest.update(entry["token"].encode("utf-8", "replace"))
        digest.update(b"\n")
    return {"algorithm": "gate-stamp-sha256-128", "fingerprint": digest.hexdigest()[:32],
            "head": gate_stamp.git_head(str(root)), "files": entries}


def workspace_token(root: Path) -> str:
    """跟踪文档也参加并发检测；未跟踪构建输入由 source_snapshot 覆盖。"""
    digest = hashlib.sha256()
    for name in sorted(filter(None, _git(root, "ls-files", "-z").split("\0"))):
        digest.update(name.encode("utf-8", "surrogateescape"))
        digest.update(b"\0")
        digest.update(gate_stamp.file_token(str(root / name)).encode())
    digest.update(source_snapshot(root)["fingerprint"].encode())
    return digest.hexdigest()


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".evidence-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("记录必须是 JSON 对象")
    return value


def validate_scenarios(record: dict[str, Any]) -> list[str]:
    scenarios = record.get("scenarios")
    if not isinstance(scenarios, list):
        return ["真实场景矩阵必须是数组"]
    errors: list[str] = []
    seen: set[Any] = set()
    allowed_status = {"passed", "failed", "unverified", "not_applicable"}
    for index, scenario in enumerate(scenarios):
        if not isinstance(scenario, dict):
            errors.append(f"scenarios[{index}] 必须是对象")
            continue
        name = scenario.get("id")
        if not isinstance(name, str):
            errors.append(f"场景 id 必须是字符串: scenarios[{index}]")
            continue
        if name in seen:
            errors.append(f"真实场景重复: {name}")
        seen.add(name)
        if not isinstance(name, str) or name not in SCENARIO_SIGNALS:
            errors.append(f"未知真实场景: {name}")
        if name in CAPABILITY_BOUNDARIES:
            boundary = CAPABILITY_BOUNDARIES[name]
            if any(scenario.get(key) != value for key, value in boundary.items()):
                errors.append(f"固定能力边界状态、required 或理由不匹配，不得声称通过: {name}")
        status = scenario.get("status")
        if not isinstance(status, str) or status not in allowed_status:
            errors.append(f"场景状态无效: {name}")
            continue
        if type(scenario.get("required")) is not bool:
            errors.append(f"场景 required 必须是布尔值: {name}")
        reason = scenario.get("reason")
        if status in {"failed", "unverified", "not_applicable"} and (
                not isinstance(reason, str) or not reason.strip()):
            errors.append(f"场景缺少非空 reason: {name}")
        if status == "not_applicable" and scenario.get("required") is not False:
            errors.append(f"场景豁免仍是必需: {name}")
        if status == "passed":
            if scenario.get("required") is not True:
                errors.append(f"通过场景必须保持 required=true: {name}")
            if not isinstance(scenario.get("receipts"), list) or not scenario["receipts"]:
                errors.append(f"通过场景缺少 receipts: {name}")
            elif any(not isinstance(ref, str) or not ref.strip() for ref in scenario["receipts"]):
                errors.append(f"场景 receipt 路径无效: {name}")
        elif ("observation" in scenario
              or ("receipts" in scenario and scenario.get("receipts"))):
            errors.append(f"未通过场景不得携带通过证据: {name}")
    expected = set(SCENARIOS)
    actual = {item.get("id") for item in scenarios
              if isinstance(item, dict) and isinstance(item.get("id"), str)}
    if len(scenarios) != len(SCENARIOS) or actual != expected or len(seen) != len(actual):
        errors.append("真实场景矩阵不完整、重复或包含未知项")
    return errors


def validate_release_policy(record: dict[str, Any]) -> list[str]:
    """Validate the closed release policy, independent of record required flags."""
    errors: list[str] = []
    scenarios = {item.get("id"): item for item in record.get("scenarios", [])
                 if isinstance(item, dict)}
    qualification = record.get("release_qualification")
    if not isinstance(qualification, dict):
        return ["正式发布资格缺少 release_qualification"]
    exclusions = qualification.get("excluded_scenarios")
    if exclusions != PERMANENT_EXEMPTIONS:
        errors.append("正式政策豁免必须严格等于用户永久授权的四项，不能新增、删除或改写")
    if qualification.get("capability_boundaries") != CAPABILITY_BOUNDARIES:
        errors.append("能力边界必须严格等于固定 Intel ARM 包不支持政策，不能新增、删除或改写")
    for name in SCENARIOS:
        item = scenarios.get(name, {})
        if name in CAPABILITY_BOUNDARIES:
            if any(item.get(key) != value for key, value in CAPABILITY_BOUNDARIES[name].items()):
                errors.append(f"固定能力边界状态、required 或理由不匹配，不得声称通过: {name}")
        elif name in PERMANENT_EXEMPTIONS:
            if item.get("status") != "not_applicable" or item.get("required") is not False:
                errors.append(f"永久排除场景必须是 not_applicable + required=false: {name}")
            if item.get("reason") != PERMANENT_EXEMPTIONS[name]:
                errors.append(f"永久排除理由不匹配: {name}")
        elif item.get("required") is not True:
            errors.append(f"非永久排除场景不得用 required=false 绕过: {name}")
    return errors


def validate(record: dict[str, Any]) -> list[str]:
    if not isinstance(record, dict):
        return ["记录必须是 JSON 对象"]
    errors = [f"缺少必填字段: {key}" for key in REQUIRED if key not in record]
    if record.get("schema_version") != SCHEMA_VERSION:
        errors.append("schema_version 必须为 1")
    for key in ("change_id", "intent"):
        if not isinstance(record.get(key), str) or not record[key].strip():
            errors.append(f"{key} 不能为空")
    for key in ("scope", "acceptance", "unverified_limitations"):
        value = record.get(key)
        if not isinstance(value, list) or not value or any(not isinstance(x, str) or not x.strip() for x in value):
            errors.append(f"{key} 必须是非空字符串数组")
    evidence = record.get("verification_evidence")
    if not isinstance(evidence, list) or not evidence:
        errors.append("verification_evidence 必须是非空对象数组")
    else:
        for index, item in enumerate(evidence):
            if not isinstance(item, dict):
                errors.append(f"verification_evidence[{index}] 必须是对象")
                continue
            if not isinstance(item.get("command"), str) or not item["command"].strip():
                errors.append(f"verification_evidence[{index}] 缺少 command")
            if type(item.get("exit_code")) is not int:
                errors.append(f"verification_evidence[{index}] exit_code 必须是整数")
    mapping = record.get("acceptance_map")
    if not isinstance(mapping, list) or any(not isinstance(item, dict) for item in mapping):
        errors.append("acceptance_map 必须是对象数组")
    else:
        for index, item in enumerate(mapping):
            if item.get("acceptance") not in record.get("acceptance", []):
                errors.append(f"acceptance_map[{index}] 引用了未知验收条件")
            if type(item.get("evidence_index")) is not int or not isinstance(evidence, list) or not (
                    0 <= item["evidence_index"] < len(evidence)):
                errors.append(f"acceptance_map[{index}] evidence_index 无效")
    artifacts = record.get("artifact_fingerprint")
    if not isinstance(artifacts, dict) or not artifacts:
        errors.append("artifact_fingerprint 必须是非空对象")
    else:
        entries = artifacts.get("artifacts", [])
        if not isinstance(entries, list):
            errors.append("artifact_fingerprint.artifacts 必须是数组")
        else:
            paths: set[str] = set()
            for index, item in enumerate(entries):
                if not isinstance(item, dict):
                    errors.append(f"artifact_fingerprint.artifacts[{index}] 必须是对象")
                    continue
                path = item.get("path")
                if not isinstance(path, str) or not path.strip():
                    errors.append(f"artifact_fingerprint.artifacts[{index}] 缺少非空 path")
                elif path in paths:
                    errors.append(f"artifact_fingerprint.artifacts 路径重复: {path}")
                else:
                    paths.add(path)
    errors.extend(validate_scenarios(record))
    return errors


def impact(root: Path, record: dict[str, Any]) -> dict[str, Any]:
    # 新建仓库没有 HEAD 时，全部已跟踪文件同样属于本次影响范围。
    if gate_stamp.git_head(str(root)) == gate_stamp.NO_GIT:
        paths = set(_git(root, "ls-files", "-z").split("\0"))
    else:
        paths = set(_git(root, "diff", "HEAD", "--name-only", "-z").split("\0"))
    paths.update(_git(root, "ls-files", "--others", "--exclude-standard", "-z").split("\0"))
    paths.discard("")
    # 核验会话自己的排他锁是运行时临时文件，不属于用户声明的源码改动。
    paths.discard("build/.quality-session.lock")
    declared = record.get("scope", [])
    # 以当前 import 图求反向闭包；动态 import 和删掉的依赖明确保留为人工复核限制。
    graph: dict[str, set[str]] = {}
    for path in input_paths(root):
        if path.suffix != ".py" or not path.exists():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        imports: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(n.name.split(".")[0] for n in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module.split(".")[0])
        graph[path.relative_to(root).as_posix()] = imports
    affected = set(paths)
    while True:
        names = {Path(p).stem for p in affected if p.endswith(".py")}
        added = {p for p, dependencies in graph.items() if dependencies & names} - affected
        if not added:
            break
        affected.update(added)
    unmapped = sorted(paths - set(declared))
    categories = []
    if any(p.startswith("tools/") or p.endswith((".sh", ".spec")) for p in affected):
        categories.append("release_chain")
    if any("config" in p or "migrat" in p or "target" in p for p in affected):
        categories.append("data_compatibility")
    if any("glass" in p or "window" in p or "render" in p for p in affected):
        categories.append("native_ui")
    return {"changed_files": sorted(paths), "affected_files": sorted(affected),
            "declared_scope": declared, "unmapped_changes": unmapped,
            "suggested_tests": sorted(p for p in affected if p.startswith("tests/test_")),
            "categories": categories,
            "test_matrix": {key: TEST_MATRIX[key] for key in categories},
            "status": "review" if unmapped else "ok",
            "limitations": ["静态 import 反向闭包不能证明动态引用、已删除依赖或系统框架影响完整；须人工确认"]}


def artifact_token(path: Path) -> dict[str, Any]:
    """文件和 .app 目录均实读；目录权限、空目录与软链接也进哈希。"""
    if not path.exists() or path.is_symlink():
        raise ValueError(f"产物缺失或顶层是软链接: {path}")
    entries: list[list[str]] = []
    paths = [path] if path.is_file() else [path, *sorted(path.rglob("*"))]
    for item in paths:
        mode = oct(item.lstat().st_mode & 0o777)
        rel = "." if item == path else item.relative_to(path).as_posix()
        if item.is_symlink():
            target = item.resolve()
            if not target.is_relative_to(path.resolve()):
                raise ValueError("产物软链接越界")
            token = "link:" + os.readlink(item)
        elif item.is_file():
            token = sha256_file(item)
        elif item.is_dir():
            token = "directory"
        else:
            raise ValueError("产物含非普通文件")
        entries.append([rel, mode, token])
    digest = hashlib.sha256(json.dumps(entries, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()
    result = {"sha256": digest, "entries": len(entries),
              "kind": "file" if path.is_file() else "directory"}
    if path.is_file():
        result["content_sha256"] = sha256_file(path)
    return result


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def now() -> str:
    return utc_now().isoformat()


def template() -> dict[str, Any]:
    return {"schema_version": 1, "change_id": "CHANGE-" + uuid.uuid4().hex[:12],
            "status": "development", "intent": "待填写", "scope": [], "acceptance": [],
            "acceptance_map": [], "verification_evidence": [],
            "artifact_fingerprint": {"source": "pending", "artifacts": []},
            "release_qualification": {"stage": "development",
                                       "policy_version": RELEASE_POLICY_VERSION,
                                       "excluded_scenarios": dict(PERMANENT_EXEMPTIONS),
                                       "capability_boundaries": {name: dict(value)
                                           for name, value in CAPABILITY_BOUNDARIES.items()}},
            "release_artifacts": {"version": "pending", "roles": {},
                                   "ota": {"archive_role": "zip",
                                            "manifest_role": "ota_manifest"}},
            "scenarios": [{"id": s,
                           "status": "not_applicable" if s in PERMANENT_EXEMPTIONS or s in CAPABILITY_BOUNDARIES else "unverified",
                           "required": s not in PERMANENT_EXEMPTIONS and s not in CAPABILITY_BOUNDARIES,
                           "reason": CAPABILITY_BOUNDARIES[s]["reason"] if s in CAPABILITY_BOUNDARIES
                           else PERMANENT_EXEMPTIONS.get(s, "尚无真实执行证据"),
                           "receipts": []} for s in SCENARIOS],
            "unverified_limitations": ["修改单尚未完成；真机、Intel、浏览器下载与线上 OTA 均未验证"]}


def run_command(root: Path, output: Path, command: list[str], level: int,
                timeout: int = 600) -> Path:
    """排他验证会话；开始/结束源码绑定，失败也留证，不沿用旧绿结果。"""
    if not command:
        raise ValueError("缺少执行命令")
    if type(level) is not int or level not in LEVELS or timeout <= 0:
        raise ValueError("测试层级或超时无效")
    output.mkdir(parents=True, exist_ok=True)
    lock_dir = root / "build"
    lock_dir.mkdir(exist_ok=True)
    with (lock_dir / ".quality-session.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("另一验证会话正在运行") from exc
        before = source_snapshot(root)
        workspace_before = workspace_token(root)
        session = output / uuid.uuid4().hex
        session.mkdir()
        logfile = session / "command.log"
        env = _git_env()
        env["SF_QUALITY_SESSION_DIR"] = str(session.resolve())
        for key, filename in {"SF_CONFIG_PATH": "config.json", "SF_LAST_QUOTE_PATH": "quote.json",
                              "SF_UNIVERSE_PATH": "universe.json", "SF_LOG_PATH": "debug.log",
                              "SF_SINGLE_SOCK": "single.sock"}.items():
            env[key] = str(session / filename)
        started = now()
        timed_out = False
        with logfile.open("wb") as stream:
            try:
                result = subprocess.run(command, cwd=root, env=env, stdout=stream,
                                        stderr=subprocess.STDOUT, timeout=timeout, check=False)
                code = result.returncode
            except subprocess.TimeoutExpired:
                code, timed_out = 124, True
            except OSError as exc:
                stream.write(str(exc).encode())
                code = 127
        after = source_snapshot(root)
        workspace_after = workspace_token(root)
        receipt = {"schema_version": 1, "producer": "quality_evidence.run", "argv": command,
                   "workspace_start": workspace_before, "workspace_end": workspace_after,
                   "workspace_stable": workspace_before == workspace_after,
                   "exit_code": code, "level": level, "started_at": started, "ended_at": now(),
                   "source_start": before["fingerprint"], "source_end": after["fingerprint"],
                   "stable": before["fingerprint"] == after["fingerprint"], "timed_out": timed_out,
                   "log": "command.log", "log_sha256": sha256_file(logfile),
                   "machine": {"system": platform.system(), "release": platform.release(),
                               "architecture": platform.machine(),
                               "device_id": hashlib.sha256(platform.node().encode()).hexdigest()}}
        destination = session / "receipt.json"
        atomic_json(destination, receipt)
        return destination


def check_receipt(path: Path, fingerprint: str) -> list[str]:
    try:
        receipt = load(path)
    except (OSError, ValueError, TypeError) as exc:
        return [f"执行凭据无法读取: {exc}"]
    errors: list[str] = []
    if type(receipt.get("schema_version")) is not int or receipt.get("schema_version") != SCHEMA_VERSION:
        errors.append("执行凭据 schema_version 无效")
    argv = receipt.get("argv")
    if (receipt.get("producer") != "quality_evidence.run" or not isinstance(argv, list)
            or not argv or any(not isinstance(arg, str) or not arg for arg in argv)):
        errors.append("不是执行器留据")
    machine = receipt.get("machine")
    if not isinstance(machine, dict) or any(not isinstance(machine.get(key), str) or not machine[key]
                                           for key in ("system", "release", "architecture", "device_id")):
        errors.append("执行设备信息缺失")
    if type(receipt.get("exit_code")) is not int or receipt["exit_code"] != 0:
        errors.append("执行未通过")
    if receipt.get("stable") is not True or receipt.get("source_start") != fingerprint or receipt.get("source_end") != fingerprint:
        errors.append("源码已变或验证期间变动")
    if receipt.get("workspace_stable") is not True or receipt.get("workspace_start") != receipt.get("workspace_end"):
        errors.append("验证期间工作区发生并发修改")
    if receipt.get("timed_out") is not False:
        errors.append("执行超时或缺少超时状态")
    if type(receipt.get("level")) is not int or receipt["level"] not in LEVELS:
        errors.append("缺少测试层级")
    try:
        log = resolve_reference(path.parent, receipt.get("log", ""))
        if not log.is_file() or sha256_file(log) != receipt.get("log_sha256"):
            errors.append("执行日志缺失或篡改")
    except (OSError, ValueError):
        errors.append("执行日志路径无效")
    errors.extend(time_errors(receipt.get("ended_at")))
    errors.extend(time_errors(receipt.get("started_at")))
    if not errors:
        if dt.datetime.fromisoformat(receipt["started_at"]) > dt.datetime.fromisoformat(receipt["ended_at"]):
            errors.append("执行时间倒置")
    return errors


def resolve_reference(base: Path, value: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("证据路径必须是非空相对路径")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("证据路径不得为绝对路径或包含上级目录")
    result = (base / relative).resolve()
    if not result.is_relative_to(base.resolve()):
        raise ValueError("证据路径越界")
    return result


def time_errors(value: Any) -> list[str]:
    try:
        moment = dt.datetime.fromisoformat(value)
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("缺少时区")
        age = utc_now() - moment
        if age.total_seconds() < 0 or age > dt.timedelta(hours=24):
            return ["证据超出 24 小时有效窗口"]
    except (TypeError, ValueError):
        return ["执行时间无效（必须为带时区 ISO-8601）"]
    return []


def manifest(root: Path, record: dict[str, Any], base: Path | None = None) -> dict[str, Any]:
    base = base or root
    snapshot = source_snapshot(root)
    artifacts = []
    for item in record["artifact_fingerprint"].get("artifacts", []):
        path = resolve_reference(base, item["path"])
        artifacts.append({"path": item["path"], **artifact_token(path)})
    if not artifacts:
        raise ValueError("没有实际产物；不能用手写 source 字段代替产物")
    result = {"schema_version": 1, "change_id": record["change_id"], "generated_at": now(),
              "source": snapshot, "artifacts": artifacts,
            "record_sha256": hashlib.sha256(json.dumps(record, sort_keys=True, ensure_ascii=True).encode()).hexdigest(),
            "limitations": record["unverified_limitations"],
            "boundary": "产物哈希只证明当前文件身份；不独自证明构建过程或线上资产同源"}
    release = record.get("release_artifacts")
    if isinstance(release, dict) and isinstance(release.get("roles"), dict) and release["roles"]:
        bound = {}
        for role, item in release["roles"].items():
            if not isinstance(item, dict):
                raise ValueError(f"release_artifacts.roles.{role} 必须是对象")
            path_value = item.get("path")
            if not isinstance(path_value, str) or not path_value.strip():
                raise ValueError(f"release_artifacts.roles.{role} 缺少 path")
            path = resolve_reference(base, path_value)
            bound[role] = {"path": path_value, **artifact_token(path)}
        result["release_artifacts"] = {"version": release.get("version"),
                                        "source_fingerprint": snapshot["fingerprint"],
                                        "roles": bound,
                                        "ota": release.get("ota")}
    return result


def scenario_errors(scenario: dict[str, Any], fingerprint: str, base: Path) -> list[str]:
    """场景结果必须提供逐项观测及附件，不接受执行层级当作场景证明。"""
    if not isinstance(scenario, dict):
        return ["场景记录不是对象"]
    name = scenario.get("id")
    if not isinstance(name, str) or name not in SCENARIO_SIGNALS:
        return ["未知真实场景"]
    status = scenario.get("status")
    if status != "passed":
        return []
    errors: list[str] = []
    observation = scenario.get("observation", {})
    if not isinstance(observation, dict):
        return [f"场景观测不是对象: {name}"]
    if observation.get("source") != fingerprint:
        errors.append(f"场景观测源码不同源: {name}")
    for key in ("device_id", "observer", "procedure", "observed_at", "artifact_sha256"):
        if not isinstance(observation.get(key), str) or not observation[key].strip():
            errors.append(f"场景观测缺少 {key}: {name}")
    errors.extend(f"{name}: {error}" for error in time_errors(observation.get("observed_at")))
    signals = observation.get("signals", {})
    if not isinstance(signals, dict):
        signals = {}
    for signal in SCENARIO_SIGNALS[name]:
        if signals.get(signal) is not True:
            errors.append(f"场景未实证 {signal}: {name}")
    attachments = observation.get("attachments", [])
    if not isinstance(attachments, list) or not attachments:
        errors.append(f"场景缺少原始附件: {name}")
    else:
        for item in attachments:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                errors.append(f"场景附件格式无效: {name}")
                continue
            try:
                path = resolve_reference(base, item["path"])
                valid = path.is_file() and sha256_file(path) == item.get("sha256")
            except (OSError, ValueError, TypeError):
                valid = False
            if not valid:
                errors.append(f"场景附件缺失或篡改: {name}")
    return errors


def verify(root: Path, record: dict[str, Any], base: Path, manifest_path: Path) -> list[str]:
    errors = validate(record)
    if errors:
        return errors
    try:
        snapshot = source_snapshot(root)
        report = impact(root, record)
    except (OSError, ValueError, SyntaxError, TypeError) as exc:
        return [f"源码或影响范围无法核验: {exc}"]
    fingerprint = snapshot["fingerprint"]
    if report["unmapped_changes"]:
        errors.append("存在未申报改动: " + ", ".join(report["unmapped_changes"]))
    evidence = record["verification_evidence"]
    receipt_cache: dict[str, tuple[Path, dict[str, Any] | None, list[str]]] = {}
    for item in evidence:
        receipt_ref = item.get("receipt")
        if not isinstance(receipt_ref, str):
            errors.append("receipt 路径无效：必须为非空字符串")
            continue
        if not receipt_ref.strip():
            errors.append("验证证据缺少真实执行 receipt")
            continue
        try:
            path = resolve_reference(base, receipt_ref)
        except (OSError, TypeError, ValueError) as exc:
            errors.append(f"{receipt_ref}: receipt 路径无效: {exc}")
            receipt_cache[receipt_ref] = (base, None, ["执行凭据路径无效"])
            continue
        reasons = check_receipt(path, fingerprint)
        try:
            receipt = load(path)
        except (OSError, TypeError, ValueError) as exc:
            receipt = None
            reasons.append(f"执行凭据无法读取: {exc}")
        argv = receipt.get("argv") if isinstance(receipt, dict) else None
        if (receipt is not None and not reasons and isinstance(argv, list)
                and all(isinstance(arg, str) and arg for arg in argv)
                and (item.get("command") != shlex.join(argv)
                     or item.get("exit_code") != receipt.get("exit_code"))):
            reasons.append("修改单与执行凭据不一致")
        receipt_cache[receipt_ref] = (path, receipt, reasons)
        errors.extend(f"{receipt_ref}: {reason}" for reason in reasons)
    mapping = record.get("acceptance_map", [])
    for acceptance in record["acceptance"]:
        rows = [x for x in mapping if isinstance(x, dict) and x.get("acceptance") == acceptance]
        if not rows or not any(type(x.get("evidence_index")) is int and 0 <= x["evidence_index"] < len(evidence) for x in rows):
            errors.append(f"验收条件没有映射执行证据: {acceptance}")
    try:
        stored = load(manifest_path)
    except (OSError, TypeError, ValueError) as exc:
        stored = None
        errors.append(f"manifest 无法读取: {exc}")
    try:
        current = manifest(root, record, base)
    except (OSError, TypeError, ValueError, KeyError) as exc:
        current = None
        errors.append(f"manifest 无法生成: {exc}")
    if stored is not None and current is not None:
        for key in ("change_id", "source", "artifacts", "record_sha256"):
            if current[key] != stored.get(key):
                errors.append(f"manifest 不一致: {key}")
    scenarios = record.get("scenarios", [])
    scenario_validation = validate_scenarios(record)
    if scenario_validation:
        errors.extend(scenario_validation)
    if not isinstance(scenarios, list):
        return errors
    valid_receipts = {ref for ref, (_, receipt, reasons) in receipt_cache.items()
                      if isinstance(ref, str) and receipt is not None and not reasons}
    artifact_hashes = ({item["sha256"] for item in current["artifacts"]}
                       if current is not None else set())
    for scenario in scenarios:
        if not isinstance(scenario, dict):
            continue
        errors.extend(scenario_errors(scenario, fingerprint, base))
        status = scenario.get("status")
        if status == "passed":
            references = scenario.get("receipts", [])
            if not references or any(ref not in valid_receipts for ref in references):
                errors.append(f"场景缺少可核对的执行证据: {scenario.get('id')}")
            for ref in references:
                cached = receipt_cache.get(ref)
                if cached is None:
                    errors.append(f"场景 receipt 未在验证证据中声明: {scenario.get('id')}")
                    continue
                receipt_path, result, receipt_reasons = cached
                if result is None or receipt_reasons:
                    continue
                observation = scenario.get("observation", {})
                if not isinstance(observation, dict):
                    observation = {}
                if observation.get("device_id") != result.get("machine", {}).get("device_id"):
                    errors.append(f"场景与执行设备不一致: {scenario.get('id')}")
                if observation.get("artifact_sha256") not in artifact_hashes:
                    errors.append(f"场景产物不属于 manifest: {scenario.get('id')}")
                if not time_errors(observation.get("observed_at")) and not check_receipt(receipt_path, fingerprint):
                    observed = dt.datetime.fromisoformat(observation["observed_at"])
                    if not (dt.datetime.fromisoformat(result["started_at"]) <= observed <= dt.datetime.fromisoformat(result["ended_at"])):
                        errors.append(f"场景观测不在执行会话时间内: {scenario.get('id')}")
                if result.get("level", 0) < 3:
                    errors.append(f"不能拿低层级替身证明真实场景: {scenario.get('id')}")
                if scenario.get("id") == "intel" and result.get("machine", {}).get("architecture") != "x86_64":
                    errors.append("Intel 场景不是 x86_64 执行证据")
        qualification = record.get("release_qualification")
        pending_post_release = (
            record.get("status") == "release_candidate"
            and isinstance(qualification, dict) and qualification.get("stage") == "pre_release"
            and scenario.get("id") in RELEASE_POST_SCENARIOS and status == "unverified")
        if scenario.get("required") is not False and status != "passed" and not pending_post_release:
            errors.append(f"必需场景尚未通过: {scenario.get('id')}")
    return errors


def verify_release_candidate(root: Path, record: dict[str, Any], base: Path,
                             manifest_path: Path) -> list[str]:
    """核验正式候选资格；开发协议通过不等于可以发布。

    发布前只要求发布前阶段的真实场景通过，发布后有效场景保持待验证。
    四专项排除与固定 Intel 能力边界不算通过；其余场景全部闭环才可 complete。
    """
    errors = verify(root, record, base, manifest_path)
    errors.extend(validate_release_policy(record))
    qualification = record.get("release_qualification")
    if record.get("status") != "release_candidate":
        errors.append("正式发布资格要求 status=release_candidate，development 记录不能冒充")
    if not isinstance(qualification, dict):
        errors.append("缺少独立 release_qualification 政策记录")
        qualification = {}
    if qualification.get("policy_version") != RELEASE_POLICY_VERSION:
        errors.append("release_qualification.policy_version 不是当前正式政策")
    exclusions = qualification.get("excluded_scenarios", {})
    if not isinstance(exclusions, dict):
        errors.append("release_qualification.excluded_scenarios 必须是对象")
        exclusions = {}
    stage = qualification.get("stage")
    if stage not in {"pre_release", "post_release", "complete"}:
        errors.append("release_qualification.stage 必须是 pre_release、post_release 或 complete")

    release = record.get("release_artifacts")
    if not isinstance(release, dict):
        errors.append("正式发布资格缺少 release_artifacts")
        release = {}
    version = release.get("version")
    if not isinstance(version, str) or not version.strip() or version == "pending":
        errors.append("正式发布资格缺少有效版本号")
    roles = release.get("roles")
    if not isinstance(roles, dict):
        errors.append("release_artifacts.roles 必须包含 app、dmg、zip、ota_manifest")
        roles = {}
    if set(roles) != set(RELEASE_ARTIFACT_ROLES):
        missing = sorted(set(RELEASE_ARTIFACT_ROLES) - set(roles))
        extra = sorted(set(roles) - set(RELEASE_ARTIFACT_ROLES))
        if missing:
            errors.append("正式产物角色缺失: " + ", ".join(missing))
        if extra:
            errors.append("正式产物角色未知: " + ", ".join(extra))
    paths: set[str] = set()
    expected_kinds = {"app": "directory", "dmg": "file", "zip": "file", "ota_manifest": "file"}
    for role in RELEASE_ARTIFACT_ROLES:
        item = roles.get(role)
        if not isinstance(item, dict):
            errors.append(f"正式产物角色无效: {role}")
            continue
        path_value = item.get("path")
        if not isinstance(path_value, str) or not path_value.strip():
            errors.append(f"正式产物角色缺少 path: {role}")
            continue
        if path_value in paths:
            errors.append(f"正式产物角色重复绑定同一路径: {path_value}")
        paths.add(path_value)
        if item.get("kind") not in (None, expected_kinds[role]):
            errors.append(f"正式产物类型错误: {role}")

    stored = None
    try:
        stored = load(manifest_path)
    except (OSError, ValueError, TypeError):
        pass
    bound = stored.get("release_artifacts") if isinstance(stored, dict) else None
    bound_roles = bound.get("roles") if isinstance(bound, dict) else None
    if not isinstance(bound_roles, dict):
        errors.append("质量 manifest 缺少正式产物角色指纹")
        bound_roles = {}
    for role in RELEASE_ARTIFACT_ROLES:
        item = roles.get(role)
        actual = bound_roles.get(role)
        if not isinstance(item, dict) or not isinstance(actual, dict):
            continue
        if actual.get("path") != item.get("path"):
            errors.append(f"正式产物路径未绑定到 manifest: {role}")
            continue
        if actual.get("kind") != expected_kinds[role]:
            errors.append(f"正式产物 manifest 类型错误: {role}")
        if not actual.get("sha256"):
            errors.append(f"正式产物缺少指纹: {role}")

    artifact_entries = record.get("artifact_fingerprint", {}).get("artifacts", [])
    declared_paths = {item.get("path") for item in artifact_entries if isinstance(item, dict)}
    for role in RELEASE_ARTIFACT_ROLES:
        item = roles.get(role)
        if isinstance(item, dict) and item.get("path") not in declared_paths:
            errors.append(f"正式产物未列入 artifact_fingerprint: {role}")

    ota = release.get("ota")
    if not isinstance(ota, dict) or ota.get("archive_role") != "zip" or ota.get("manifest_role") != "ota_manifest":
        errors.append("release_artifacts.ota 必须明确绑定 zip 与 ota_manifest")
    else:
        ota_path = roles.get("ota_manifest", {}).get("path") if isinstance(roles.get("ota_manifest"), dict) else None
        zip_path = roles.get("zip", {}).get("path") if isinstance(roles.get("zip"), dict) else None
        if isinstance(ota_path, str) and isinstance(zip_path, str):
            try:
                ota_data = json.loads(resolve_reference(base, ota_path).read_text(encoding="utf-8"))
                zip_file = resolve_reference(base, zip_path)
                if ota_data.get("version") != version:
                    errors.append("OTA 清单版本与正式产物版本不一致")
                archive = ota_data.get("archive")
                if not isinstance(archive, dict):
                    errors.append("OTA 清单缺少 archive 对象")
                else:
                    raw_hash = sha256_file(zip_file)
                    if archive.get("sha256") != raw_hash:
                        errors.append("OTA 清单 SHA-256 与 ZIP 原始字节不一致")
                    if type(archive.get("size")) is not int or archive["size"] != zip_file.stat().st_size:
                        errors.append("OTA 清单大小与 ZIP 原始字节不一致")
                    if not isinstance(archive.get("url"), str) or not archive["url"].endswith(zip_file.name):
                        errors.append("OTA 清单下载地址未绑定正式 ZIP")
            except (OSError, ValueError, TypeError, KeyError) as exc:
                errors.append(f"OTA 清单无法核验: {exc}")

    scenarios = {item.get("id"): item for item in record.get("scenarios", [])
                 if isinstance(item, dict)}
    for name in RELEASE_PRE_SCENARIOS:
        if scenarios.get(name, {}).get("status") != "passed":
            errors.append(f"正式候选缺少发布前必需场景: {name}")
    for name in RELEASE_POST_SCENARIOS:
        status = scenarios.get(name, {}).get("status")
        if status == "failed":
            errors.append(f"发布后必需场景已失败，不能取得正式资格: {name}")
        elif status == "not_applicable":
            reason = exclusions.get(name)
            if not isinstance(reason, str) or not reason.strip():
                errors.append(f"发布后场景排除必须有独立理由: {name}")
        elif status not in {"unverified", "passed"}:
            errors.append(f"发布后场景状态无效: {name}")
    if stage in {"post_release", "complete"} and any(
            scenarios.get(name, {}).get("status") != "passed" for name in RELEASE_POST_SCENARIOS):
        errors.append("发布后阶段未完成全部真实场景，不能标记为 " + stage)
    if stage == "complete" and any(
            scenarios.get(name, {}).get("status") != "passed" for name in RELEASE_REQUIRED_SCENARIOS):
        errors.append("全部有效必需场景未通过，不能标记正式交付 complete")
    return errors


def qualification_report(root: Path, record: dict[str, Any], base: Path,
                         manifest_path: Path) -> dict[str, Any]:
    """生成不带歧义的资格结果；阻塞项保留，不能用异常或空报告代替。"""
    try:
        source = source_snapshot(root)
    except (OSError, ValueError, SyntaxError, TypeError) as exc:
        source = {"error": str(exc)}
    try:
        errors = verify_release_candidate(root, record, base, manifest_path)
    except (OSError, ValueError, KeyError, TypeError, SyntaxError) as exc:
        errors = [f"正式资格核验异常: {exc}"]
    qualification = record.get("release_qualification")
    release = record.get("release_artifacts")
    scenarios = record.get("scenarios")
    scenario_status = {
        item.get("id"): item.get("status")
        for item in scenarios if isinstance(item, dict) and isinstance(item.get("id"), str)
    } if isinstance(scenarios, list) else {}
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now(),
        "status": "qualified" if not errors else "blocked",
        "change_id": record.get("change_id"),
        "source": source,
        "policy": {
            "version": RELEASE_POLICY_VERSION,
            "stage": qualification.get("stage") if isinstance(qualification, dict) else None,
            "pre_release_scenarios": list(RELEASE_PRE_SCENARIOS),
            "post_release_scenarios": list(RELEASE_POST_SCENARIOS),
            "required_scenarios": list(RELEASE_REQUIRED_SCENARIOS),
            "permanent_exemptions": dict(PERMANENT_EXEMPTIONS),
            "capability_boundaries": CAPABILITY_BOUNDARIES,
            "artifact_roles": list(RELEASE_ARTIFACT_ROLES),
            "excluded_scenarios": qualification.get("excluded_scenarios", {})
            if isinstance(qualification, dict) else {},
        },
        "release": {
            "status": record.get("status"),
            "version": release.get("version") if isinstance(release, dict) else None,
            "artifact_roles": sorted(release.get("roles", {}).keys())
            if isinstance(release, dict) and isinstance(release.get("roles"), dict) else [],
            "scenarios": scenario_status,
        },
        "blockers": errors,
        "limitations": record.get("unverified_limitations", []),
        "boundary": "本报告只反映当前本地证据；不证明真实设备、线上服务或授权后的实际交付",
    }


def post_release_record(root: Path, candidate: Path, release_log: Path,
                        installed_app: Path) -> Path:
    """Retrospective readback, not a newly minted historical execution receipt."""
    import plistlib

    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    import sf_update

    record = load(candidate)
    bound_path = candidate.with_suffix(".manifest.json")
    bound = load(bound_path)
    version = record["release_artifacts"]["version"]
    try:
        sf_update.parse_version(version)
    except sf_update.UpdateError as exc:
        raise ValueError("发布后记录版本无效") from exc
    output = root / "outputs" / "release-candidates" / ("v" + version)
    target = output / "post_release.json"
    if target.exists():
        raise ValueError("不覆盖现有发布后记录")
    expected_record = hashlib.sha256(json.dumps(
        record, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
    if expected_record != bound.get("record_sha256"):
        raise ValueError("候选记录与历史 manifest 不一致")
    roles = bound["release_artifacts"]["roles"]
    actual = {}
    for role, item in roles.items():
        token = artifact_token(resolve_reference(root, item["path"]))
        if token != {key: value for key, value in item.items() if key != "path"}:
            raise ValueError("发布产物已变化: " + role)
        actual[role] = item
    installed = artifact_token(installed_app)
    if installed != {key: value for key, value in roles["app"].items() if key != "path"}:
        raise ValueError("装机包与候选包不一致")
    with (installed_app / "Contents" / "Info.plist").open("rb") as stream:
        identity = plistlib.load(stream)
    if identity.get("CFBundleShortVersionString") != version:
        raise ValueError("装机版本不一致")
    signature = subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict",
                                str(installed_app)], capture_output=True, text=True, timeout=30)
    if signature.returncode:
        raise ValueError("装机签名无效: " + signature.stderr)
    log = release_log.read_text(encoding="utf-8")
    markers = ["已安装: " + str(installed_app), "版本: " + version,
               "published_archive=Skyrocket-" + version + "-macos.zip",
               "最终启动复验：" + str(installed_app), "结论: 冒烟通过",
               "发版完成 v" + version]
    if any(marker not in log for marker in markers):
        raise ValueError("发布日志缺少安装、发布或最终启动证据")
    try:
        raw = sf_update.fetch_manifest()
        info = sf_update.verify_manifest(raw)
    except sf_update.UpdateError as exc:
        raise ValueError("线上清单读取或验签失败") from exc
    local = resolve_reference(root, roles["ota_manifest"]["path"]).read_bytes()
    if raw != local or info.version != version:
        raise ValueError("线上签名清单与已绑定产物不一致")
    if info.archive_sha256 != roles["zip"]["content_sha256"]:
        raise ValueError("线上清单 ZIP 哈希不一致")
    scenarios = []
    for item in record["scenarios"]:
        item = dict(item)
        if item["id"] in CAPABILITY_BOUNDARIES:
            item.update(CAPABILITY_BOUNDARIES[item["id"]], receipts=[])
            item.pop("observation", None)
        elif item["id"] in PERMANENT_EXEMPTIONS:
            item.update(status="not_applicable", required=False,
                        reason=PERMANENT_EXEMPTIONS[item["id"]], receipts=[])
        elif item["status"] == "not_applicable":
            item.update(status="unverified", required=True,
                        reason="不允许把非永久政策专项标记为不适用；尚无真实场景证据")
        scenarios.append(item)
    output.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(release_log, output / "publish.log")
    (output / "online-manifest.json").write_bytes(raw)
    value = {"schema_version": 1, "stage": "post_release", "status": "incomplete",
             "version": version, "recorded_at": now(),
             "evidence_kind": "retrospective_readback_not_execution_receipt",
             "candidate_sha256": sha256_file(candidate),
             "candidate_manifest_sha256": sha256_file(bound_path),
             "release_source": {key: bound["source"][key]
                                for key in ("head", "fingerprint", "algorithm")},
             "artifacts": actual,
             "installed": {"path": str(installed_app), **installed,
                           "version": version, "bundle_id": identity.get("CFBundleIdentifier"),
                           "signature_valid": True},
             "publication": {"signed_manifest_verified": True, "matches_local_bytes": True,
                             "manifest_sha256": hashlib.sha256(raw).hexdigest(),
                             "archive_sha256": info.archive_sha256},
             "historical_publish_log": {"path": "publish.log", "sha256": sha256_file(release_log),
                                        "matched_markers": markers,
                                        "timestamp_boundary": "日志未提供结束时间；不由 mtime 推算"},
             "scenarios": scenarios,
             "permanent_exemptions": dict(PERMANENT_EXEMPTIONS),
             "capability_boundaries": {name: dict(value) for name, value in CAPABILITY_BOUNDARIES.items()},
             "unverified_limitations": ["未完成全部真实场景，不能进入 complete",
                 "发布日志不是 quality_evidence.run 回执；只保留原始事实与当前读回",
                 "发布后新改动不属于此已发布包；不刷新历史源码指纹",
                 "历史候选路径均相对仓库根；归档不重签或刷新资格"],
             "pending_scenarios": [item["id"] for item in scenarios
                                   if item["id"] in RELEASE_REQUIRED_SCENARIOS
                                   and item["status"] != "passed"]}
    atomic_json(target, value)
    return target


def self_test() -> int:
    good = {"schema_version": 1, "change_id": "T", "intent": "测试", "scope": ["x.py"],
            "acceptance": ["可执行"], "acceptance_map": [{"acceptance": "可执行", "evidence_index": 0}],
            "verification_evidence": [{"command": "true", "exit_code": 0}],
            "artifact_fingerprint": {"source": "pending", "artifacts": [{"path": "x.bin"}]},
            "scenarios": template()["scenarios"],
            "unverified_limitations": ["无真机证据"]}
    cases = [("valid", good, False)]
    cases += [(f"missing {key}", {k: v for k, v in good.items() if k != key}, True) for key in REQUIRED]
    cases += [("bool exit", {**good, "verification_evidence": [{"command": "true", "exit_code": True}]}, True),
              ("string evidence", {**good, "verification_evidence": ["passed"]}, True)]
    failed = 0
    for name, record, expected in cases:
        ok = bool(validate(record)) == expected
        print(f"[{'OK' if ok else '!!'}] {name}")
        failed += not ok
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "artifact"
        path.write_bytes(b"first")
        original = artifact_token(path)
        path.write_bytes(b"second")
        ok = original != artifact_token(path)
        print(f"[{'OK' if ok else '!!'}] artifact tamper")
        failed += not ok
    return int(bool(failed))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", choices=("init", "validate", "snapshot", "impact", "manifest", "verify", "qualify", "qualification-report", "run", "post-release"))
    parser.add_argument("record", nargs="?")
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--level", type=int, choices=tuple(LEVELS), default=1)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--release-log", type=Path)
    parser.add_argument("--installed-app", type=Path, default=Path("/Applications/Skyrocket.app"))
    raw = list(sys.argv[1:] if argv is None else argv)
    command_args = raw[raw.index("--") + 1:] if "--" in raw else []
    args = parser.parse_args(raw[:raw.index("--")] if "--" in raw else raw)
    if args.self_test:
        return self_test()
    if not args.command or not args.record:
        parser.error("需要 command 和 record；run 的 record 参数为证据输出目录")
    root, path = Path(args.root).resolve(), Path(args.record).resolve()
    try:
        if args.command == "post-release":
            if args.release_log is None:
                raise ValueError("post-release 需要 --release-log")
            print(post_release_record(root, path, args.release_log, args.installed_app))
            return 0
        if args.command == "init":
            if path.exists():
                raise ValueError("不覆盖现有修改单")
            atomic_json(path, template())
            print(path)
            return 0
        if args.command == "run":
            result = run_command(root, path, command_args, args.level, args.timeout)
            print(result)
            receipt = load(result)
            errors = check_receipt(result, receipt["source_start"])
            for error in errors:
                print("!! " + error)
            return int(bool(errors))
        record = load(path)
        if args.command == "validate":
            errors = validate(record)
        elif args.command == "verify":
            errors = verify(root, record, path.parent, path.with_suffix(".manifest.json"))
        elif args.command == "qualify":
            errors = verify_release_candidate(root, record, path.parent,
                                              path.with_suffix(".manifest.json"))
        elif args.command == "qualification-report":
            value = qualification_report(root, record, path.parent,
                                         path.with_suffix(".manifest.json"))
            output = path.with_suffix(".qualification-report.json")
            atomic_json(output, value)
            print(output)
            return 0 if value["status"] == "qualified" else 1
        else:
            if args.command == "snapshot":
                value = source_snapshot(root)
            elif args.command == "impact":
                value = impact(root, record)
            else:
                errors = validate(record)
                if errors:
                    raise ValueError("; ".join(errors))
                value = manifest(root, record, path.parent)
            output = path.with_suffix(f".{args.command}.json")
            atomic_json(output, value)
            print(output)
            return 0
        for error in errors:
            print("!! " + error)
        if not errors:
            print("== 质量证据通过 ==")
        return 1 if errors else 0
    except (OSError, ValueError, KeyError, TypeError, SyntaxError) as exc:
        print(f"!! 无法完成质量核验: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
