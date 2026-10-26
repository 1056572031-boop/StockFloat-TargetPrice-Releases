"""File-only diagnostic collection and compression, independent of Qt widgets."""
import datetime
import json
import os
import platform
import time
import zipfile

from sf_config import _read_json_or_none, redact_config


class DiagnosticArchive:
    def __init__(self, path):
        self.path = path
        self.entries: dict[str, bytes] = {}
        self.callbacks: list = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def writestr(self, name, data):
        self.entries[name] = data.encode("utf-8") if isinstance(data, str) else bytes(data)

    def infolist(self):
        entries = []
        for name, data in self.entries.items():
            entry = zipfile.ZipInfo(name)
            entry.file_size = len(data)
            entries.append(entry)
        return entries

    def defer(self, callback):
        self.callbacks.append(callback)

    def finish(self):
        for callback in self.callbacks:
            callback()
        with zipfile.ZipFile(self.path, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
            for name, data in self.entries.items():
                archive.writestr(name, data)
        self.entries.clear()
        self.callbacks.clear()


def write_attachments(archive, paths, metadata, issues, helpers):
    """Only data/callables with no QWidget access cross this worker boundary."""
    json_entry = helpers["json_entry"]
    text_entry = helpers["text_entry"]
    read_text = helpers["read_text"]
    public_text = helpers["public_text"]
    field = helpers["field"]

    def storage_file(path):
        try:
            details = os.stat(path)
            return {"exists": True, "size_bytes": details.st_size,
                    "modified": datetime.datetime.fromtimestamp(details.st_mtime).isoformat(timespec="seconds")}
        except FileNotFoundError:
            return {"exists": False}
        except OSError as exc:
            return {"error": public_text(exc)}

    json_entry("storage.json", {
        "config": storage_file(paths["config"]),
        "config_backup": storage_file(paths["backup"]),
        "last_quote_cache": storage_file(paths["last_quote"]),
        "updates": field("updates", helpers["inventory"], {}, "storage")})
    config = _read_json_or_none(paths["config"])
    if config is not None:
        json_entry("config.json", redact_config(config))
    else:
        issues.append("config.json: missing or unreadable")
    log_arcs = [(paths["log"], "debug.log")]
    for number in range(1, metadata["log_keep"] + 1):
        log_arcs.append((paths["log"] + ".%d" % number, "debug.log.%d" % number))
    for source, name in log_arcs:
        if not os.path.exists(source):
            issues.append("%s: missing" % name)
            continue
        try:
            text = read_text(source, name, metadata["log_max"], tail=True)
            text_entry(name, public_text(text), metadata["log_max"], tail=True)
        except Exception as exc:
            issues.append("%s: %s" % (name, public_text(exc)))
    failures = paths["failures"]
    if os.path.exists(failures):
        try:
            raw = read_text(failures, "last_failures.json")
            try:
                json_entry("last_failures.json", redact_config(json.loads(raw)))
            except json.JSONDecodeError:
                text_entry("last_failures.invalid.txt", public_text(raw))
                issues.append("last_failures.json: invalid JSON")
        except Exception as exc:
            issues.append("last_failures.json: %s" % public_text(exc))
    else:
        issues.append("last_failures.json: 无记录（说明，非故障："
                      "本次运行没有留痕的静默失效，或产物早于 v1.4.33）")
    crash_summary = []
    crash_dir = paths["crashes"]
    if os.path.isdir(crash_dir):
        try:
            candidates = []
            for name in os.listdir(crash_dir):
                if helpers["is_crash"](name):
                    try:
                        candidates.append((name, os.path.getmtime(os.path.join(crash_dir, name))))
                    except OSError as exc:
                        issues.append("%s: %s" % (name, public_text(exc)))
            for name, modified in helpers["select_crashes"](candidates):
                stamp = datetime.datetime.fromtimestamp(modified).strftime("%Y-%m-%d %H:%M:%S")
                crash_summary.append("%s @ %s" % (name, stamp))
                try:
                    body = read_text(os.path.join(crash_dir, name), "crash_reports/%s" % name,
                                     8 * 1024 * 1024)
                except Exception as exc:
                    issues.append("crash_reports/%s: %s" % (name, public_text(exc)))
                    body = ""
                body = public_text(helpers["extract_crash"](body))
                if body:
                    archive.writestr("crash_reports/%s.txt" % name, body)
        except Exception as exc:
            issues.append("crash_reports: %s" % public_text(exc))
    overview = ("APP_VERSION=%s\nsystem=%s\ncode=%s\n"
                "last_ok_time=%s\nquote_source=%s\nlast_http_status=%s\n"
                "RELEASE_STAGE=%s\nVERSION_LABEL=%s\nBUNDLE_ID=%s\nRUNNING_BUNDLE_ID=%s\n"
                "crash_reports:\n%s\nexport_issues:\n%s\n" % (
                    metadata["version"], field("system", platform.platform, "unknown", "diagnostics"),
                    metadata["code"], metadata["last_ok_time"], metadata["quote_source"],
                    metadata["http_status"], metadata["stage"], metadata["version_label"],
                    metadata["bundle_id"], metadata["running_bundle_id"],
                    "\n".join(crash_summary) or "(none)", "\n".join(issues) or "(none)"))
    archive.writestr("diagnostics.txt", public_text(overview))
    export = {
        "version": metadata["version"],
        "exported_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "collection_ms": round((time.monotonic() - metadata["started"]) * 1000, 1),
        "issues": list(issues),
        "files": [{"name": info.filename, "size_bytes": info.file_size}
                  for info in archive.infolist()],
    }
    if metadata.get("diagnostic_schema_version") is not None:
        export["diagnostic_schema_version"] = metadata["diagnostic_schema_version"]
    json_entry("export.json", export)
