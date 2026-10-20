"""本机候选交付核验：隔离冒烟、临时首装、真实 helper 升级与故障回退。

不写 /Applications，不代表浏览器下载/Gatekeeper 或跨机验证通过。
所有 helper 目标及 verification-home 都在同一私有系统临时根内。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
import quality_evidence as evidence
HELPER = ROOT / "updater" / "update_helper.sh"


def run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, capture_output=True, text=True,
                   errors="replace", timeout=180)


def metadata(app: Path) -> dict:
    if app.name != "Skyrocket.app" or not app.is_dir():
        raise ValueError("候选顶层必须是 Skyrocket.app 目录")
    with (app / "Contents" / "Info.plist").open("rb") as fh:
        info = plistlib.load(fh)
    if not isinstance(info, dict):
        raise ValueError("Info.plist 不是字典")  # noqa: TRY004 -- malformed external plist
    name = info.get("CFBundleExecutable")
    if not isinstance(name, str) or not name or name in (".", "..") or "/" in name:
        raise ValueError("CFBundleExecutable 非法")
    exe = app / "Contents" / "MacOS" / name
    if not exe.is_file() or not exe.resolve().is_relative_to(app.resolve()):
        raise ValueError("可执行文件不存在或逃出候选包")
    if info.get("CFBundleIdentifier") != "com.skyrocket.app":
        raise ValueError("候选 bundle id 不匹配")
    version = info.get("CFBundleShortVersionString")
    if not isinstance(version, str) or not version:
        raise ValueError("候选版本缺失")
    return info


def preflight(app: Path, archive: Path, root: Path) -> dict:
    expected = metadata(app)
    if not archive.is_file() or archive.suffix.lower() != ".zip":
        raise ValueError("--zip 必须是 zip 文件")
    copied = root / "copy" / "Skyrocket.app"
    copied.parent.mkdir()
    run(["/usr/bin/ditto", str(app), str(copied)])
    for bundle in (app, copied):
        run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(bundle)])
    return {"version": expected["CFBundleShortVersionString"],
            "app_and_copy_signatures_valid": True, "copied_with": "ditto",
            "archive_verified": False}


def smoke(app: Path) -> dict:
    # Import is lazy so pure temporary self-tests never load or launch a bundle.
    import importlib
    sys.path.insert(0, str(ROOT / "tools"))
    smoke_frozen_app = importlib.import_module("smoke_frozen_app")
    rc, report = smoke_frozen_app.run_smoke(
        app, platform="offscreen", timeout=35, keep_dir=True)
    if (rc != 0 or not report.get("started") or not report.get("startup")
            or not report.get("first_frame") or report.get("returncode") not in (0, -15, -2)):
        raise RuntimeError("隔离冒烟未通过: " + json.dumps(report, ensure_ascii=False))
    return report


def require_helper(helper: Path) -> None:
    # This is a capability preflight, not proof of runtime isolation. Runtime
    # result/log/sentinel checks below must still succeed; never try old helpers.
    source = helper.read_text(encoding="utf-8")
    for marker in ("--verification-home)", "validate_verification_home", "launch_app",
                   "/usr/bin/open -n -g", "--env \"SF_CONFIG_PATH=", "--env SF_ALLOW_TEMP_LAUNCH=1"):
        if marker not in source:
            raise RuntimeError("helper 隔离接口未就绪，拒绝启动候选: " + marker)


def process_rows() -> list[tuple[int, str]]:
    result = subprocess.run(["/bin/ps", "-axo", "pid=,comm="], check=True,
                            capture_output=True, text=True, timeout=10)
    return [(int(parts[0]), parts[1]) for line in result.stdout.splitlines()
            if len(parts := line.strip().split(None, 1)) == 2]


def stop_target(app: Path, executable: str) -> None:
    """Only exact executable paths under our private temporary target may be signalled."""
    target = app.resolve()
    temp = Path(tempfile.gettempdir()).resolve()
    if not target.is_relative_to(temp) or target.name != "Skyrocket.app":
        raise ValueError("拒绝清理非临时目标")
    exact = str(target / "Contents" / "MacOS" / executable)
    for pid, command in process_rows():
        if command != exact or pid == os.getpid():
            continue
        # Recheck immediately before SIGTERM, never pkill/regex/substring matching.
        if (pid, exact) in process_rows():
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                continue  # process exited between identity check and SIGTERM
    deadline = time.monotonic() + 10
    while any(command == exact for _, command in process_rows()):
        if time.monotonic() >= deadline:
            raise RuntimeError("临时目标 SIGTERM 后仍在运行: " + exact)
        time.sleep(0.1)


def sentinel(home: Path) -> tuple[Path, bytes]:
    home.mkdir()
    config = home / "config.json"
    content = (json.dumps({"delivery_sentinel": "must-not-change", "code": "sh600000",
                          "x": 120, "y": 120, "h": 320, "bg": "glass",
                          "alerts": [{"code": "sh600000", "direction": "above", "target": 999999.0,
                                      "session_date": "2099-01-05", "state": "pending", "last_side": None}]},
                         sort_keys=True) + "\n").encode()
    config.write_bytes(content)
    return config, content


def check_sentinel(config: Path, content: bytes) -> None:
    if config.is_symlink():
        raise RuntimeError("sentinel 配置被修改")
    actual = config.read_bytes()
    if actual != content:
        raise RuntimeError("sentinel 配置被修改: " + json.dumps({
            "expected": content.decode("utf-8", errors="replace")[:65536],
            "actual": actual.decode("utf-8", errors="replace")[:65536],
            "expected_sha256": hashlib.sha256(content).hexdigest(),
            "actual_sha256": hashlib.sha256(actual).hexdigest()}, ensure_ascii=False))


def readback_config(config: Path, content: bytes, app: Path | None = None) -> dict:
    """在新进程用实际包内解析器（或源码对照）读回；不冒充界面验收。"""
    check_sentinel(config, content)
    expected = json.loads(content)
    home = config.parent.resolve()
    if not home.is_relative_to(Path(tempfile.gettempdir()).resolve()):
        raise ValueError("配置读回只允许私有临时目录")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SF_", "GIT_"))}
    env.update(HOME=str(home), SF_CONFIG_PATH=str(config), SF_LOG_PATH=str(home / "readback.log"),
               SF_UNIVERSE_PATH=str(home / "securities.json"), SF_LAST_QUOTE_PATH=str(home / "last_quote.json"))
    command = [str(ROOT / ".venv/bin/python3"), "-c",
               ("import json; from sf_config import load_config, load_alerts; "
                "print(json.dumps({'window': load_config(), 'alerts': load_alerts('sh600000')}))")]
    if app is not None:
        command = [str(ROOT / ".venv/bin/python3"), str(ROOT / "tools/config_bundle_readback.py"),
                   "--app", str(app), "--config", str(config)]
    result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True,
                            check=True, timeout=30)
    observed = json.loads(result.stdout)
    bundle_version = observed.pop("bundle_version", None)
    if app is not None and bundle_version != metadata(app)["CFBundleShortVersionString"]:
        raise RuntimeError("配置解析器的包内版本与应用不一致")
    wanted = {"window": [expected[key] for key in ("code", "x", "y", "h", "bg")],
              "alerts": expected["alerts"]}
    if observed != wanted:
        raise RuntimeError("目标价或窗口设置无法完整读回")
    check_sentinel(config, content)
    return {"scope": "frozen_bundle_parser_new_process_not_ui" if app else "current_source_parser_new_process_not_ui",
            "bundle_version": bundle_version, "observed": observed}


def helper_route(archive: Path, version: str, old: Path, root: Path,
                 rollback: bool = False) -> dict:
    root.mkdir()
    target = root / "Skyrocket.app"
    run(["/usr/bin/ditto", str(old), str(target)])
    old_info = metadata(target)
    old_exe = Path("Contents") / "MacOS" / old_info["CFBundleExecutable"]
    old_digest = hashlib.sha256((target / old_exe).read_bytes()).hexdigest()
    run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(target)])
    config, content = sentinel(root / "home")
    baseline_readback = readback_config(config, content, target)
    result = root / "result.json"
    # A waited child supplies a genuinely dead PID, not an assumed-unused number.
    child = subprocess.Popen(["/usr/bin/true"])
    child.wait(timeout=10)
    command = ["/bin/sh", str(HELPER), "--pid", str(child.pid),
               "--archive", str(archive), "--sha256", hashlib.sha256(archive.read_bytes()).hexdigest(),
               "--version", version, "--target", str(target), "--result", str(result),
               "--verification-home", str(config.parent)]
    env = {k: v for k, v in os.environ.items() if not k.startswith("SF_")}
    env.update(HOME=str(config.parent), TMPDIR=str(root), LANG="zh_CN.UTF-8")
    try:
        completed = subprocess.run(command, env=env, capture_output=True, check=False,
                                   text=True, errors="replace", timeout=150)
        outcome = json.loads(result.read_text(encoding="utf-8"))
        expected = {"status": "rolled_back", "message": "restart_new_app_failed"} if rollback else {
            "status": "installed", "message": "ok"}
        if outcome != expected or completed.returncode != (1 if rollback else 0):
            raise RuntimeError("helper 结果不符: " + str(outcome))
        info = metadata(target)
        if rollback:
            if info != old_info or hashlib.sha256((target / old_exe).read_bytes()).hexdigest() != old_digest:
                raise RuntimeError("恢复的旧包 metadata/可执行内容不符")
        else:
            metadata_archive_expected(version, info)
        run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(target)])
    finally:
        # Both the upgraded executable and the restored old executable are ours.
        for name in {old_info["CFBundleExecutable"], "DeliveryShortLived",
                     metadata(target)["CFBundleExecutable"]}:
            stop_target(target, name)
        check_sentinel(config, content)
    # In particular rollback must prove a real old bundle can still start/paint,
    # not merely that a directory or a helper status exists.
    observed = smoke(target)
    check_sentinel(config, content)
    data_readback = readback_config(config, content, target)
    return {"helper": outcome, "helper_returncode": completed.returncode,
            "smoke": observed, "sentinel_unchanged": True,
            "data_readback": data_readback,
            "baseline_data_readback": baseline_readback,
            "version": info["CFBundleShortVersionString"]}


def metadata_archive_expected(version: str, info: dict) -> dict:
    if info["CFBundleShortVersionString"] != version:
        raise RuntimeError("升级目标版本不符")
    return info


def fault_archive(root: Path, version: str) -> Path:
    """Correct archive layout/signature, but deliberately exits before liveness check."""
    app = root / "fault" / "Skyrocket.app"
    exe = app / "Contents" / "MacOS" / "DeliveryShortLived"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    exe.chmod(0o755)
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps({
        "CFBundleExecutable": exe.name, "CFBundleIdentifier": "com.skyrocket.app",
        "CFBundleShortVersionString": version, "CFBundlePackageType": "APPL",
        "LSUIElement": True}))
    run(["/usr/bin/codesign", "--force", "--sign", "-", str(app)])
    run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)])
    archive = root / "fault.zip"
    run(["/usr/bin/ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(app), str(archive)])
    return archive


def verify(app: Path, archive: Path, out: Path, old: Path | None = None) -> int:
    report: dict = {"verified": False, "app": str(app), "zip": str(archive),
              "smoke": "not_run", "first_install": "not_run", "upgrade": "not_run",
              "rollback": "not_run", "scope": "本机临时安装；不代表跨机或 Gatekeeper 通过"}
    report["matrix"] = {name: {"status": "unverified"} for name in
                        ("candidate_startup", "fresh_install", "upgrade_install", "ota_forward",
                         "ota_rollback", "user_data_readback", "old_app_restart")}
    report["data_readback_boundary"] = "关键配置由实际旧包、升级包或回滚旧包内的解析器在新进程读回；不证明界面已显示这些设置"
    try:
        report["artifacts"] = {"app": evidence.artifact_token(app), "zip": evidence.artifact_token(archive)}
        with tempfile.TemporaryDirectory(prefix="sf_cd_") as tmp:
            root = Path(tmp).resolve()
            report["preflight"] = preflight(app, archive, root)
            require_helper(HELPER)
            if old is None:
                raise ValueError("必须提供 --old-app 真正已签名旧包，不能用候选冒充升级")
            old_info = metadata(old)
            if old_info["CFBundleShortVersionString"] == metadata(app)["CFBundleShortVersionString"]:
                raise ValueError("旧包版本必须不同于候选")
            run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(old)])
            report["smoke"] = smoke(app)
            report["matrix"]["candidate_startup"]["status"] = "passed"
            report["first_install"] = smoke(root / "copy" / "Skyrocket.app")
            report["matrix"]["fresh_install"]["status"] = "passed"
            version = report["preflight"]["version"]
            report["upgrade"] = helper_route(archive, version, old, root / "upgrade")
            for name in ("upgrade_install", "ota_forward"):
                report["matrix"][name]["status"] = "passed"
            report["preflight"]["archive_verified"] = True
            report["rollback"] = helper_route(fault_archive(root, version), version, old,
                                                root / "rollback", rollback=True)
            for name in ("ota_rollback", "user_data_readback", "old_app_restart"):
                report["matrix"][name]["status"] = "passed"
            report["verified"] = True
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        report["error"] = str(exc)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["verified"] else 1


def self_test() -> int:
    """Real negative file judgments; no app/helper/process launch."""
    with tempfile.TemporaryDirectory(prefix="sf_delivery_selftest_") as tmp:
        root = Path(tmp)
        config, content = sentinel(root / "home")
        check_sentinel(config, content)
        config.write_bytes(b"corrupted")
        rejected = 0
        for fn, args in ((check_sentinel, (config, content)), (metadata, (root,)),
                         (require_helper, (config,))):
            try:
                fn(*args)
            except (ValueError, RuntimeError):
                rejected += 1
        if rejected != 3:
            print("自检失败：负向输入被放过")
            return 1
    print("自检通过：配置篡改、错误顶层、缺失隔离接口均拒绝；不等于候选通过")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("app", "zip", "out", "old-app"):
        parser.add_argument("--" + name)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    if args.self_test:
        return self_test()
    if not args.app or not args.zip or not args.out or not args.old_app:
        parser.error("必须同时提供 --app --zip --out --old-app")
    return verify(Path(args.app).resolve(), Path(args.zip).resolve(), Path(args.out).resolve(),
                  Path(args.old_app).resolve())


if __name__ == "__main__":
    sys.exit(main())
