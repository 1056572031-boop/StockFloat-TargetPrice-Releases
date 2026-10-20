"""Admin-only polling queue and credential-free, sandboxed report generation."""

from __future__ import annotations

import fcntl
import hashlib
import json
import resource
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

from sf_feedback import FeedbackProtocolError, _atomic_replace, validate_archive, validate_receipt
from sf_feedback_github import KEY_PATTERN, REPOSITORY
from sf_feedback_analyzer import ANALYZER_BUILD_ID, DEFAULT_RULESET, _canonical_hash


def _limits():
    resource.setrlimit(resource.RLIMIT_CPU, (15, 15))
    resource.setrlimit(resource.RLIMIT_FSIZE, (8 * 1024 * 1024, 8 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))


def analyze_isolated(archive, output):
    """No credentials in args/env; macOS sandbox denies network and Keychain."""
    sandbox = Path("/usr/bin/sandbox-exec")
    if not sandbox.is_file():
        raise FeedbackProtocolError("隔离分析器不可用，不降级为无隔离分析")
    root = Path(__file__).resolve().parent
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    home = Path.home()
    python_root = Path(vars(sys).get("base_prefix", sys.prefix)).resolve()
    def literal(path):
        return json.dumps(str(path), ensure_ascii=False)
    # Source is trusted and archive data is never executed. The process is
    # additionally denied all network, Keychain reads and credential services.
    profile = ('(version 1)(allow default)(deny network*)'
               '(deny mach-lookup (global-name "com.apple.securityd"))'
               '(deny file-read* (subpath %s) (subpath %s) (subpath %s))'
               '(deny file-read-data (require-all (subpath %s)'
               ' (require-not (subpath %s)) (require-not (subpath %s))'
               ' (require-not (subpath %s)) (require-not (subpath %s))))'
               '(deny file-write* (require-not (subpath %s)))') % (
                   literal(home / "Library/Keychains"), literal(home / ".config/gh"),
                   literal(home / ".ssh"), literal(home), literal(root), literal(python_root),
                   literal(Path(archive).resolve().parent), literal(output), literal(output))
    code = ("import sys,json; sys.path.insert(0,sys.argv[1]); "
            "from sf_feedback_analyzer import analyze_archive; "
            "r=analyze_archive(sys.argv[2],sys.argv[3]); "
            "print(json.dumps({k:v for k,v in r.items() if k != 'full_diagnostics'}))")
    interpreter = str(Path(vars(sys).get("_base_executable", sys.executable)).resolve())
    command = [str(sandbox), "-p", profile, interpreter, "-I", "-B", "-c", code,
               str(root), str(Path(archive).resolve()), str(output)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False,
                            env={"PATH": "/usr/bin:/bin", "HOME": str(output),
                                 "LANG": "en_US.UTF-8"}, preexec_fn=_limits)
    if result.returncode:
        raise FeedbackProtocolError("隔离分析失败，退出码 %d" % result.returncode)
    report = json.loads(result.stdout)
    for key in ("json_path", "markdown_path"):
        path = Path(report[key]).resolve()
        if not path.is_relative_to(output) or not path.is_file():
            raise FeedbackProtocolError("分析报告没有真实落盘")
    return report


class FeedbackAgent:
    def __init__(self, api, directory, *, analyzer=analyze_isolated):
        self.api = api
        self.root = Path(directory).resolve()
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.root.chmod(0o700)
        self.analyzer = analyzer
        self.database = self._local_path("queue.sqlite3")
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS queue (
                case_id TEXT PRIMARY KEY, receipt TEXT NOT NULL,
                status TEXT NOT NULL, error TEXT, report TEXT)""")

    def _local_path(self, *parts):
        path = self.root.joinpath(*parts)
        if not path.is_relative_to(self.root):
            raise FeedbackProtocolError("反馈路径越界")
        current = self.root
        for part in path.relative_to(self.root).parts:
            current = current / part
            if part == ".." or current.is_symlink():
                raise FeedbackProtocolError("反馈路径包含越界或符号链接")
        return path

    def _report_paths(self, report, case_id):
        report_root = self._local_path("reports", case_id)
        paths = [self._local_path(report[key]) for key in ("json_path", "markdown_path")]
        if (paths[0] == paths[1] or any(path.parent != report_root or not path.is_file()
                                      for path in paths)):
            raise FeedbackProtocolError("报告没有在案件目录真实落盘")
        return paths

    def _cache_valid(self, report, receipt, archive, case_id):
        paths = self._report_paths(report, case_id)
        hashes = report.get("artifact_sha256", {})
        return (report.get("analyzer_build_id") == ANALYZER_BUILD_ID
                and report.get("ruleset_sha256") == _canonical_hash(DEFAULT_RULESET)
                and report.get("archive_sha256") == receipt["archive_sha256"]
                and archive.is_file() and archive.stat().st_size == receipt["size_bytes"]
                and hashlib.sha256(archive.read_bytes()).hexdigest() == receipt["archive_sha256"]
                and set(hashes) == {str(path) for path in paths}
                and all(hashlib.sha256(path.read_bytes()).hexdigest() == hashes[str(path)]
                        for path in paths))

    @contextmanager
    def _db(self):
        for name in ("queue.sqlite3", "queue.sqlite3-wal", "queue.sqlite3-shm"):
            self._local_path(name)
        db = sqlite3.connect(self.database)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    def run_once(self):
        lock = self._local_path("agent.lock").open("a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            raise FeedbackProtocolError("已有反馈代理正在运行") from None
        try:
            return self._run_locked()
        finally:
            lock.close()

    def _run_locked(self):
        release = self.api.inbox()
        assets = self.api.assets(release["id"])
        by_id = {a["id"]: a for a in assets}
        # Persist all discovered cases before processing. No advancing cursor:
        # failed downloads remain in the queue even across restarts/page shifts.
        for asset in assets:
            name = asset["name"]
            if not name.startswith("feedback-") or not name.endswith(".receipt.json"):
                continue
            key = name[len("feedback-"):-len(".receipt.json")]
            if not KEY_PATTERN.fullmatch(key):
                continue
            case_id = "feedback-" + key
            with self._db() as db:
                existing = db.execute("SELECT status FROM queue WHERE case_id=?", (case_id,)).fetchone()
                if existing and existing[0] not in ("discovery_failed", "failed"):
                    continue
            try:
                receipt = json.loads(self.api.download(asset["id"], limit=64 * 1024))
                if (receipt.get("case_id") != case_id or receipt.get("repository") != REPOSITORY
                        or receipt.get("storage_status") != "stored" or receipt.get("validation_status") != "validated"):
                    raise FeedbackProtocolError("远端反馈回执身份或状态不匹配")
                validate_receipt(receipt, receipt)
            except Exception as exc:
                with self._db() as db:
                    db.execute("INSERT INTO queue VALUES (?, '{}', 'discovery_failed', ?, NULL) "
                               "ON CONFLICT(case_id) DO UPDATE SET status='discovery_failed', error=excluded.error",
                               (case_id, type(exc).__name__))
                continue
            with self._db() as db:
                db.execute("INSERT INTO queue VALUES (?, ?, 'pending', NULL, NULL) "
                           "ON CONFLICT(case_id) DO UPDATE SET receipt=excluded.receipt, status='pending', error=NULL",
                           (case_id, json.dumps(receipt)))
        with self._db() as db:
            rows = db.execute("SELECT case_id, receipt, status, report FROM queue").fetchall()
        ready, failed = [], []
        for case_id, raw_receipt, status, previous_report in rows:
            if (not isinstance(case_id, str) or not case_id.startswith("feedback-")
                    or not KEY_PATTERN.fullmatch(case_id[len("feedback-"):])):
                with self._db() as db:
                    db.execute("UPDATE queue SET status='failed', error='InvalidCaseIdentity' WHERE case_id IS ?", (case_id,))
                failed.append("invalid_local_case")
                continue
            if status == "discovery_failed":
                failed.append(case_id)
                continue
            try:
                receipt = json.loads(raw_receipt)
                if (receipt.get("case_id") != case_id or receipt.get("repository") != REPOSITORY
                        or receipt.get("storage_status") != "stored" or receipt.get("validation_status") != "validated"):
                    raise FeedbackProtocolError("本地反馈回执身份或状态不匹配")
                validate_receipt(receipt, receipt)
                path = self._local_path("objects", case_id + ".zip")
                report_root = self._local_path("reports", case_id)
                if status == "ready":
                    old = json.loads(previous_report)
                    try:
                        cached = self._cache_valid(old, receipt, path, case_id)
                    except (FeedbackProtocolError, KeyError, OSError):
                        cached = False
                    if cached:
                        continue
                asset = by_id.get(receipt.get("asset_id"))
                if (asset is None or asset["name"] != case_id + ".zip"
                        or asset["size"] != receipt["size_bytes"]):
                    raise FeedbackProtocolError("案件对象缺失或大小不匹配")
                body = self.api.download(asset["id"], limit=receipt["size_bytes"])
                if (len(body) != receipt["size_bytes"]
                        or hashlib.sha256(body).hexdigest() != receipt["archive_sha256"]):
                    raise FeedbackProtocolError("下载的反馈包校验失败")
                _atomic_replace(path, lambda stream, data=body: stream.write(data))
                validate_archive(path)
                with self._db() as db:
                    db.execute("UPDATE queue SET status='analyzing', error=NULL WHERE case_id=?", (case_id,))
                report = self.analyzer(path, report_root)
                if report.get("archive_sha256") != receipt["archive_sha256"]:
                    raise FeedbackProtocolError("报告未绑定当前反馈包")
                paths = self._report_paths(report, case_id)
                report["artifact_sha256"] = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
                with self._db() as db:
                    db.execute("UPDATE queue SET status='ready', error=NULL, report=? WHERE case_id=?",
                               (json.dumps(report), case_id))
                ready.append({"case_id": case_id, "conclusion": report["conclusion"],
                              "report": report["markdown_path"]})
            except Exception as exc:
                with self._db() as db:
                    db.execute("UPDATE queue SET status='failed', error=? WHERE case_id=?",
                               (type(exc).__name__, case_id))
                failed.append(case_id)
        return {"ready": ready, "failed": failed, "discovered": len(rows)}
