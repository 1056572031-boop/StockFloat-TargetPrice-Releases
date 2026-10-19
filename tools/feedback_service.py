#!/usr/bin/env python3
"""Loopback HTTP receiver; production requires an independently configured TLS proxy."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import struct
import subprocess
import sys
import threading
from datetime import datetime, timezone
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sf_feedback_github import GitHubAPI, GitHubInbox
from sf_feedback_transport import MAX_UPLOAD_BYTES


class Admission:
    def __init__(self, database, *, daily_limit=100):
        self.database = str(database)
        self.daily_limit = daily_limit
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("CREATE TABLE IF NOT EXISTS requests (day TEXT PRIMARY KEY, count INTEGER)")

    def admit(self):
        day = datetime.now(timezone.utc).date().isoformat()
        with closing(sqlite3.connect(self.database, isolation_level=None)) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR IGNORE INTO requests VALUES (?, 0)", (day,))
            count = db.execute("SELECT count FROM requests WHERE day=?", (day,)).fetchone()[0]
            if count >= self.daily_limit:
                db.rollback()
                return False
            db.execute("UPDATE requests SET count=count+1 WHERE day=?", (day,))
            db.commit()
        return True


def make_server(inbox, admission, *, port=0):
    capacity = threading.BoundedSemaphore(2)

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            self.request.settimeout(15)
            super().setup()

        def log_message(self, format, *args):
            return

        def do_POST(self):
            if self.path != "/v1/feedback":
                self.send_error(404)
                return
            if (self.headers.get("Transfer-Encoding")
                    or len(self.headers.get_all("Content-Length", [])) != 1
                    or self.headers.get("Content-Type") != "application/vnd.skyrocket.feedback-v1"):
                self.send_error(400)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self.send_error(400)
                return
            if not 4 < length <= MAX_UPLOAD_BYTES + 64 * 1024 + 4:
                self.send_error(413)
                return
            if not capacity.acquire(blocking=False):
                self.send_error(503)
                return
            try:
                if not admission.admit():
                    self.send_response(429)
                    self.send_header("Retry-After", "3600")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.connection.settimeout(30)
                body = self.rfile.read(length)
                if len(body) != length:
                    raise ValueError("incomplete body")
                size = struct.unpack("!I", body[:4])[0]
                if not 0 < size <= 64 * 1024 or 4 + size >= len(body):
                    raise ValueError("invalid envelope")
                envelope = json.loads(body[4:4 + size])
                receipt = inbox.accept(envelope, body[4 + size:])
                data = json.dumps(receipt).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception:
                self.send_error(422, "feedback not acknowledged; retry with the same identity")
            finally:
                capacity.release()

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8769)
    parser.add_argument("--gh", required=True, help="Absolute path to administrator gh executable")
    args = parser.parse_args()
    os.umask(0o077)
    args.data_dir.mkdir(parents=True, exist_ok=True)
    token = subprocess.run([args.gh, "auth", "token"], capture_output=True, text=True,
                           check=True, timeout=15).stdout.strip()
    inbox = GitHubInbox(GitHubAPI(token))
    inbox.api.inbox()
    server = make_server(inbox, Admission(args.data_dir / "admission.sqlite3"), port=args.port)
    print("Loopback feedback receiver ready; HTTPS proxy required for remote clients.", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
