"""A real process that can fail shortly after logging its first frame."""
import os
import sys
import threading

with open(os.environ["SF_LOG_PATH"], "w", encoding="utf-8") as log:
    log.write("STARTUP vprobe run=probe\nfirst-frame painted 184x224\n")
    log.flush()
if sys.argv[1] == "fail":
    threading.Event().wait(0.4)
    raise SystemExit(6)
threading.Event().wait(30)
