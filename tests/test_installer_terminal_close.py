"""Exercise the shipped terminal closer without touching the user's windows."""

import json
import os
import subprocess
import unittest
from pathlib import Path
from tests._base import SFCase

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = next((ROOT / "installer").glob("*.command"))


def closer_sources():
    body = SCRIPT.read_text(encoding="utf-8")
    _, start, rest = body.partition("# BEGIN close_install_terminal\n")
    shell, end, _ = rest.partition("# END close_install_terminal")
    if not start or not end:
        raise AssertionError("The shipped terminal closer markers are missing")
    _, start, rest = shell.partition("<<'INSTALL_TERMINAL_JXA'")
    _, newline, rest = rest.partition("\n")
    javascript, end, _ = rest.partition("\nINSTALL_TERMINAL_JXA\n")
    if not start or not newline or not end:
        raise AssertionError("The shipped JXA source is missing")
    return shell, javascript


class TestInstallerTerminalClose(SFCase):
    def simulate(self, windows, **options):
        _, javascript = closer_sources()
        scenario = {"windows": windows, "options": options}
        prelude = """
var scenario = SCENARIO;
var closed = [], tick = 0;
var terminalFactory = function () {
    var model = {
        running: function () { return !scenario.options.stopped; },
        windows: function () {
            return scenario.windows.filter(function (w) {
                return !(w.disappear && tick > 0);
            }).map(function (w) {
                return {
                    id: function () { return w.id; },
                    tabs: function () {
                        var tabs = w.tabs;
                        if (w.addTab && tick > 0) tabs = tabs.concat([{}]);
                        return tabs.map(function (t) {
                            return {
                                tty: function () {
                                    return w.changeTTY && tick > 0 ? "other" : t.tty;
                                },
                                busy: function () { return t.busy && tick < (t.end || 99); },
                                processes: function () {
                                    return tick < (t.end || 99) ? (t.processes || []) : [];
                                }
                            };
                        });
                    },
                    close: function (args) { closed.push({id: w.id, args: args}); }
                };
            });
        }
    };
    model.windows.byId = function (id) {
        return model.windows().filter(function (w) { return w.id() === id; })[0];
    };
    return model;
};
var testDelay = function () { tick++; };
""".replace("SCENARIO", json.dumps(scenario))
        # Use the shipped algorithm with a fake Terminal; never send real Apple events.
        harness = prelude + javascript.replace("function run(argv)", "function productionRun(argv)", 1)
        harness += '\nJSON.stringify({result: closeInstallWindow(terminalFactory(), "/dev/ttys123", testDelay), closed: closed, ticks: tick});'
        proc = subprocess.run(
            ["/usr/bin/osascript", "-l", "JavaScript", "-e", harness],
            capture_output=True, text=True, timeout=15, check=True)
        return json.loads(proc.stdout)

    @staticmethod
    def window(identifier=1, **tab):
        return {"id": identifier, "tabs": [{"tty": "/dev/ttys123", **tab}]}

    def test_closes_only_the_matching_finished_window(self):
        other = {"id": 2, "tabs": [{"tty": "/dev/ttys999"}]}
        result = self.simulate([other, self.window()])
        self.assertEqual(result["closed"], [{"id": 1, "args": {"saving": "no"}}])

    def test_waits_for_installation_shell_to_finish(self):
        result = self.simulate([self.window(busy=True, processes=["sh"], end=3)])
        self.assertEqual(result["ticks"], 3)
        self.assertEqual(len(result["closed"]), 1)

    def test_manual_shell_is_not_closed_even_when_not_busy(self):
        result = self.simulate([self.window(processes=["zsh"])])
        self.assertEqual(result["closed"], [])
        self.assertEqual(result["result"], "install-window-still-in-use")
        self.assertEqual(result["ticks"], 30)

    def test_busy_window_is_never_closed(self):
        self.assertEqual(self.simulate([self.window(busy=True)])["closed"], [])

    def test_window_with_other_tabs_is_not_closed(self):
        window = self.window()
        window["tabs"].append({"tty": "/dev/ttys999"})
        self.assertEqual(self.simulate([window])["closed"], [])

    def test_new_tab_during_wait_preserves_the_window(self):
        window = self.window(busy=True, end=1)
        window["addTab"] = True
        self.assertEqual(self.simulate([window])["closed"], [])

    def test_changed_tty_during_wait_preserves_the_window(self):
        window = self.window(busy=True, end=1)
        window["changeTTY"] = True
        self.assertEqual(self.simulate([window])["closed"], [])

    def test_already_closed_window_is_harmless(self):
        window = self.window(busy=True)
        window["disappear"] = True
        self.assertEqual(self.simulate([window])["result"], "install-window-already-closed")

    def test_missing_tty_does_not_close_another_window(self):
        window = {"id": 2, "tabs": [{"tty": "/dev/ttys999"}]}
        self.assertEqual(self.simulate([window])["closed"], [])

    def test_stopped_terminal_is_not_launched(self):
        self.assertEqual(self.simulate([self.window()], stopped=True)["closed"], [])

    def test_piped_installation_does_not_start_automation(self):
        shell, _ = closer_sources()
        proc = subprocess.run(
            ["/bin/sh", "-c", "set -u\n" + shell + "\nclose_install_terminal"],
            input="", capture_output=True, text=True,
            env=dict(os.environ, TERM_PROGRAM="Apple_Terminal"), timeout=10, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout + proc.stderr, "")

    def test_non_apple_terminal_does_not_start_automation(self):
        shell, _ = closer_sources()
        proc = subprocess.run(
            ["/bin/sh", "-c", "set -u\n" + shell + "\nclose_install_terminal"],
            capture_output=True, text=True,
            env=dict(os.environ, TERM_PROGRAM="iTerm.app"), timeout=10, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout + proc.stderr, "")

    def test_closer_is_called_only_after_successful_launch_and_detach(self):
        body = SCRIPT.read_text(encoding="utf-8")
        call = body.rindex("\nclose_install_terminal\n")
        self.assertLess(body.index('if [ "${SF_INSTALL_NO_LAUNCH:-0}" = "1" ]'), call)
        self.assertLess(body.index('/usr/bin/open "$DEST" || die_next_step'), call)
        self.assertLess(body.index('/usr/bin/hdiutil detach "$SELF_DIR"'), call)
        self.assertEqual(body[call:].strip(), "close_install_terminal\nexit 0")


if __name__ == "__main__":
    unittest.main()
