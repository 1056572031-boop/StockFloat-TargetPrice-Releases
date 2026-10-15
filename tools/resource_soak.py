"""Time-bounded macOS resource observation, with opt-in application launch.

No application imports, UI automation, camera access, power assertions or sudo.
CPU deltas exclude sampling gaps and process restarts; battery drain is not read.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import plistlib
import resource
import statistics
import subprocess
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
THERMAL_NAMES = {0: "nominal", 1: "fair", 2: "serious", 3: "critical"}
LIMITATIONS = [
    "AC-only observation; no battery discharge or endurance test.",
    "No controlled app-off baseline; system load is not attributable to the app.",
    "Thermal state is a system pressure level, not a temperature in Celsius.",
    "Temperature and fan RPM are unavailable in this unprivileged collector.",
    "An invisible window does not prove minimization to Dock.",
    "The face-guard preference does not prove an active camera session.",
    "Sampling gaps do not prove sleep or successful wake recovery.",
    "RSS is resident memory, not total process memory or proof of a leak.",
]


def command(args):
    return subprocess.run(args, capture_output=True, check=True, timeout=10).stdout


def cpu_seconds(value):
    days, sep, clock = value.partition("-")
    if not sep:
        days, clock = "0", days
    parts = [float(x) for x in clock.split(":")]
    if len(parts) not in (2, 3) or any(x < 0 for x in parts):
        raise ValueError("invalid CPU time")
    seconds = sum(x * 60 ** i for i, x in enumerate(reversed(parts)))
    return int(days) * 86400 + seconds


def parse_processes(text):
    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        fields = line.split(None, 10)
        if len(fields) != 11:
            raise ValueError("incomplete process row")
        rows.append({
            "pid": int(fields[0]), "ppid": int(fields[1]),
            "ps_cpu_percent": float(fields[2]), "rss_kib": int(fields[3]),
            "cpu_seconds": cpu_seconds(fields[4]),
            "started": " ".join(fields[5:10]), "executable": fields[10],
        })
    return rows


def process_tree(rows, executable):
    selected = {r["pid"] for r in rows if r["executable"] == executable}
    roots = sorted(selected)
    while True:
        expanded = selected | {r["pid"] for r in rows if r["ppid"] in selected}
        if expanded == selected:
            return roots, [r for r in rows if r["pid"] in selected]
        selected = expanded


def power_source(text):
    first = text.splitlines()[0] if text.splitlines() else ""
    if "'AC Power'" in first:
        return "ac"
    if "'Battery Power'" in first:
        return "battery_excluded"
    return "unknown"


def read_bundle(app):
    with (app / "Contents/Info.plist").open("rb") as stream:
        info = plistlib.load(stream)
    name = info["CFBundleExecutable"]
    if not name or Path(name).name != name:
        raise ValueError("invalid bundle executable")
    return {"version": info.get("CFBundleShortVersionString"),
            "bundle_id": info.get("CFBundleIdentifier"),
            "executable": str(app / "Contents/MacOS" / name)}


def running_app_pids(app):
    bundle = read_bundle(app)
    rows = parse_processes(command([
        "/bin/ps", "-axo", "pid=,ppid=,%cpu=,rss=,time=,lstart=,comm=",
    ]).decode("utf-8", "replace"))
    return process_tree(rows, bundle["executable"])[0]


def ensure_app_running(app, *, timeout=30, clock=time.monotonic, sleep=time.sleep,
                       runner=command, detector=None):
    detector = detector or (lambda: running_app_pids(app))
    pids = detector()
    if pids:
        return {"action": "already_running", "verified_pids": pids}
    deadline = clock() + timeout
    runner(["/usr/bin/open", "-a", str(app)])
    while clock() < deadline:
        pids = detector()
        if pids:
            return {"action": "launched", "verified_pids": pids}
        sleep(min(0.5, max(0, deadline - clock())))
    raise TimeoutError("installed application process did not appear after launch")


def collect(app):
    errors = []

    def optional(name, reader):
        try:
            return reader()
        except Exception as exc:  # noqa: BLE001 - report optional metric failures independently
            errors.append({"metric": name, "error": type(exc).__name__})
            return None

    bundle = read_bundle(app)
    rows = parse_processes(command([
        "/bin/ps", "-axo", "pid=,ppid=,%cpu=,rss=,time=,lstart=,comm=",
    ]).decode("utf-8", "replace"))
    roots, processes = process_tree(rows, bundle["executable"])
    for row in processes:
        row.pop("executable")

    def screen():
        import Quartz
        session = Quartz.CGSessionCopyCurrentDictionary()
        if session is None:
            raise RuntimeError("session unavailable")
        windows = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID)
        if windows is None:
            raise RuntimeError("window list unavailable")
        return {
            "locked": bool(session.get("CGSSessionScreenIsLocked", False)),
            "display_asleep": bool(Quartz.CGDisplayIsAsleep(Quartz.CGMainDisplayID())),
            "visible_app_windows": sum(
                1 for w in windows if w.get(Quartz.kCGWindowOwnerPID) in roots
                and w.get(Quartz.kCGWindowLayer) == 0),
        }

    def idle():
        data = plistlib.loads(command(["/usr/sbin/ioreg", "-a", "-r", "-c", "IOHIDSystem"]))
        return data[0]["HIDIdleTime"] / 1e9

    def thermal():
        from Foundation import NSProcessInfo
        return THERMAL_NAMES.get(int(NSProcessInfo.processInfo().thermalState()), "unknown")

    def guard_preference():
        config = Path.home() / "Library/Application Support/StockFloatTargetPrice/config.json"
        if not config.exists():
            return False
        return bool(json.loads(config.read_text(encoding="utf-8")).get("face_guard", False))

    context = sorted(
        (r for r in rows if r["pid"] not in {p["pid"] for p in processes}
         and r["pid"] != os.getpid()), key=lambda r: r["ps_cpu_percent"], reverse=True)
    return {
        "epoch": time.time(), "timestamp": dt.datetime.now(TZ).isoformat(),
        "bundle": {k: v for k, v in bundle.items() if k != "executable"},
        "app_roots": roots, "app_processes": processes,
        "rss_kib": sum(p["rss_kib"] for p in processes) if roots else None,
        "power_source": optional("power_source", lambda: power_source(
            command(["/usr/bin/pmset", "-g", "batt"]).decode("utf-8", "replace"))),
        "screen": optional("screen", screen), "idle_seconds": optional("idle", idle),
        "thermal_state": optional("thermal_state", thermal),
        "face_guard_configured": optional("face_guard_preference", guard_preference),
        "temperature_celsius": None, "fan_rpm": None,
        "context_top_cpu": [{"name": Path(r["executable"]).name,
                             "ps_cpu_percent": r["ps_cpu_percent"]} for r in context[:5]],
        "errors": errors,
    }


def interval_metrics(previous, current, interval):
    result = {"gap_seconds": None, "app_cpu_percent": None}
    if previous is None:
        return result
    elapsed = current["epoch"] - previous["epoch"]
    if elapsed <= 0 or elapsed > interval * 1.5:
        result["gap_seconds"] = elapsed
        return result
    if previous["power_source"] != "ac" or current["power_source"] != "ac":
        return result
    old = {(p["pid"], p["started"]): p["cpu_seconds"] for p in previous["app_processes"]}
    new = {(p["pid"], p["started"]): p["cpu_seconds"] for p in current["app_processes"]}
    if not old or old.keys() != new.keys():
        return result
    delta = sum(new[k] - old[k] for k in old)
    if delta >= 0:
        result["app_cpu_percent"] = delta / elapsed * 100
    return result


def summarize(samples):
    valid = [s for s in samples if s.get("app_roots") and s.get("power_source") == "ac"]
    cpu = [s["app_cpu_percent"] for s in valid if s.get("app_cpu_percent") is not None]
    rss = [s["rss_kib"] / 1024 for s in valid]
    return {
        "samples": len(samples), "valid_ac_app_samples": len(valid),
        "app_missing_samples": sum(not s.get("app_roots") for s in samples),
        "excluded_non_ac_samples": sum(s.get("power_source") != "ac" for s in samples),
        "cpu_intervals": len(cpu), "cpu_mean_percent": statistics.mean(cpu) if cpu else None,
        "cpu_peak_percent": max(cpu) if cpu else None,
        "rss_min_mib": min(rss) if rss else None, "rss_max_mib": max(rss) if rss else None,
        "rss_first_to_last_mib": rss[-1] - rss[0] if rss else None,
        "gap_seconds": [s["gap_seconds"] for s in samples if s.get("gap_seconds") is not None],
        "samples_with_metric_errors": sum(bool(s.get("errors")) for s in samples),
        "thermal_states": sorted({s["thermal_state"] for s in valid if s.get("thermal_state")}),
        "limitations": LIMITATIONS,
    }


def write_json(path, data):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def aware_time(value):
    result = dt.datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("timestamps must include timezone")
    return result.astimezone(TZ)


def launch_agent(python, script, app, output, start, end, interval, label, *, launch_if_missing=False):
    agent = {
        "Label": label,
        "ProgramArguments": [str(python), str(script), "run", "--app", str(app),
                             "--output", str(output), "--start", start.isoformat(),
                             "--end", end.isoformat(), "--interval", str(interval)],
        "StartCalendarInterval": {"Month": start.month, "Day": start.day,
                                  "Hour": start.hour, "Minute": start.minute},
        "EnvironmentVariables": {"TZ": "Asia/Shanghai", "PYTHONUNBUFFERED": "1"},
        "ProcessType": "Background", "Nice": 10,
        "StandardOutPath": str(output / "observer.stdout.log"),
        "StandardErrorPath": str(output / "observer.stderr.log"),
    }
    if launch_if_missing:
        agent["ProgramArguments"].append("--launch-if-missing")
    return agent


def observe(app, output, start, end, interval, *, clock=time.time, sleep=time.sleep,
            reader=collect, launcher=None):
    now = clock()
    if now < start.timestamp():
        raise ValueError("scheduled window has not started")
    output.mkdir(parents=True, exist_ok=True)
    status_path = output / "status.json"
    if now >= end.timestamp():
        if not (output / "samples.jsonl").exists():
            write_json(status_path, {"status": "missed_window", "samples": 0})
        return 0
    if (output / "samples.jsonl").exists():
        raise FileExistsError("existing samples must not be overwritten")
    lock = output / ".running"
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    samples: list[dict] = []
    status = {"status": "running", "scheduled_start": start.isoformat(),
              "scheduled_end": end.isoformat(), "actual_start_epoch": now,
              "observer_pid": os.getpid()}
    cpu_start = resource.getrusage(resource.RUSAGE_SELF)
    try:
        write_json(status_path, status)
        if launcher is not None:
            status.update(status="starting_app", app_launch={"action": "requested"})
            write_json(status_path, status)
            status["app_launch"] = launcher(app, timeout=min(30, end.timestamp() - clock()))
            status["status"] = "running"
            write_json(status_path, status)
        # Exclusive creation protects an earlier run from accidental rescheduling.
        with (output / "samples.jsonl").open("x", encoding="utf-8") as stream:
            while clock() < end.timestamp():
                tick = clock()
                sample = reader(app)
                sample.update(interval_metrics(samples[-1] if samples else None, sample, interval))
                samples.append(sample)
                stream.write(json.dumps(sample, ensure_ascii=True) + "\n")
                stream.flush()
                status.update(summarize(samples))
                status["last_sample_epoch"] = sample["epoch"]
                write_json(status_path, status)
                sleep(max(0, min(tick + interval, end.timestamp()) - clock()))
        status["status"] = "completed" if status["valid_ac_app_samples"] else "no_valid_app_data"
        return 0
    except BaseException as exc:
        status.update(status="failed", error=type(exc).__name__)
        raise
    finally:
        cpu_end = resource.getrusage(resource.RUSAGE_SELF)
        status.update(finished_epoch=clock(), observer_cpu_seconds=(
            cpu_end.ru_utime + cpu_end.ru_stime - cpu_start.ru_utime - cpu_start.ru_stime))
        write_json(status_path, status)
        lock.unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("probe", "prepare", "run"))
    parser.add_argument("--app", type=Path, default=Path("/Applications/Skyrocket.app"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--start", type=aware_time)
    parser.add_argument("--end", type=aware_time)
    parser.add_argument("--interval", type=float, default=60)
    parser.add_argument("--label", default="com.skyrocket.resource-soak-20261015-0300")
    parser.add_argument("--agent-dir", type=Path, default=Path.home() / "Library/LaunchAgents")
    parser.add_argument("--launch-if-missing", action="store_true")
    args = parser.parse_args(argv)
    if not math.isfinite(args.interval) or not 10 <= args.interval <= 3600:
        parser.error("interval must be between 10 and 3600 seconds")
    if args.mode == "probe":
        print(json.dumps(collect(args.app), indent=2))
        return 0
    if args.output is None or args.start is None or args.end is None or args.end <= args.start:
        parser.error("output, timezone-aware start and a later end are required")
    if args.mode == "run":
        return observe(args.app, args.output, args.start, args.end, args.interval,
                       launcher=ensure_app_running if args.launch_if_missing else None)
    if args.start.second or args.start.microsecond:
        parser.error("launchd calendar starts must be aligned to a minute")
    args.output.mkdir(parents=True, exist_ok=True)
    args.agent_dir.mkdir(parents=True, exist_ok=True)
    agent = launch_agent(sys.executable, Path(__file__).resolve(), args.app.resolve(),
                         args.output.resolve(), args.start, args.end, args.interval, args.label,
                         launch_if_missing=args.launch_if_missing)
    plist = args.agent_dir / (args.label + ".plist")
    with plist.open("xb") as stream:
        plistlib.dump(agent, stream)
    write_json(args.output / "schedule.json", agent)
    write_json(args.output / "status.json", {"status": "prepared", "scheduled_start": args.start.isoformat(),
                                           "scheduled_end": args.end.isoformat(), "samples": 0})
    print(plist)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
