"""Power assertion observability and a real independent cold-start process."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from sf_runtime import NapTokenManager, RuntimeState
from tests._base import SFCase
from tests.test_runtime import FakeProcessInfo


class RuntimeQualityTests(SFCase):
    def test_power_diagnostics_do_not_expose_the_activity_object(self):
        info = FakeProcessInfo()
        runtime = RuntimeState(NapTokenManager(ns_process_info=info))
        runtime.update_nap(True)
        payload = runtime.diagnostics()
        self.assertTrue(payload["activity"]["active"])
        self.assertNotIn("token", payload["activity"])
        self.assertFalse(payload["activity"]["activity_options"] & (1 << 20))
        runtime.shutdown()
        self.assertFalse(runtime.diagnostics()["activity"]["active"])

    def test_failed_activity_changes_are_observable(self):
        manager = NapTokenManager(ns_process_info=FakeProcessInfo(fail_begin=True))
        manager.set_trading(True)
        payload = manager.diagnostics()
        self.assertEqual(payload["failure_count"], 1)
        self.assertEqual(payload["last_error"], "RuntimeError")
        self.assertFalse(payload["active"])

    def test_coldstart_tool_produces_a_real_first_frame_in_a_fresh_process(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="sf_cold_") as temp:
            output = Path(temp) / "result.json"
            env = dict(os.environ, QT_QPA_PLATFORM="offscreen", SF_UNIVERSE_DISABLE="1")
            for key in ("SF_CONFIG_PATH", "SF_LAST_QUOTE_PATH", "SF_LOG_PATH", "SF_SINGLE_SOCK", "SF_UNIVERSE_PATH"):
                env[key] = str(Path(temp) / key)
            process = subprocess.run([sys.executable, str(root / "tools/coldstart_bench.py"),
                                      "--one-run", "--src", str(root), "--json", str(output)],
                                     env=env, capture_output=True, text=True, timeout=25)
            self.assertEqual(process.returncode, 0, process.stderr[-1200:])
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertGreater(payload["t_first_frame"], 0)
            self.assertGreater(payload["first_ink"], 0)
