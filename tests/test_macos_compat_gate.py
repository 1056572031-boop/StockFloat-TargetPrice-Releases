"""Binary compatibility uses deployment load commands, including negative controls."""
import io
from pathlib import Path
import plistlib
import struct
import tempfile
from contextlib import redirect_stdout, redirect_stderr
from unittest import mock

from tests._base import SFCase
from tools import verify_macos_compat as gate


class MacOSBundleGate(SFCase):
    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory(prefix="sf_compat_test_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_real_arm64_fixture_supports_both_target_systems(self):
        app = gate.sample_bundle(self.root)
        report = gate.check_bundle(app)
        self.assertEqual(report["issues"], [])
        self.assertEqual(report["images"], 1)
        self.assertEqual(report["minimum"], (13, 0, 0))

    def test_sdk_is_not_confused_with_minimum_os(self):
        report = gate.check_bundle(gate.sample_bundle(self.root))
        self.assertFalse(report["issues"], "SDK 27 can build a binary deployed to macOS 13")

    def test_every_embedded_image_must_fit_declared_system_range(self):
        app = gate.sample_bundle(self.root)
        foreign = gate.sample_bundle(self.root / "other", minimum=27, declared="27.0")
        nested = app / "Contents/Frameworks/New.dylib"
        nested.parent.mkdir()
        nested.write_bytes((foreign / "Contents/MacOS/Probe").read_bytes())
        report = gate.check_bundle(app)
        self.assertEqual(report["images"], 2)
        self.assertTrue(any("New.dylib" in item and "macOS 26" in item for item in report["issues"]))

    def test_wrong_arch_and_wrong_platform_are_not_called_compatible(self):
        for index, kwargs in enumerate([{"cpu": 0x01000007}, {"platform": 2}]):
            with self.subTest(kwargs=kwargs):
                report = gate.check_bundle(gate.sample_bundle(self.root / str(index), **kwargs))
                self.assertTrue(report["issues"])

    def test_old_deployment_load_command_is_supported(self):
        app = gate.sample_bundle(self.root)
        binary = app / "Contents/MacOS/Probe"
        header = struct.pack("<8I", 0xFEEDFACF, gate.ARM64, 0, 2, 1, 16, 0, 0)
        binary.write_bytes(header + struct.pack("<4I", gate.LC_VERSION_MIN_MACOSX, 16, 13 << 16, 26 << 16))
        self.assertEqual(gate.check_bundle(app)["issues"], [])

    def test_absolute_build_machine_library_is_rejected(self):
        app = gate.sample_bundle(self.root)
        binary = app / "Contents/MacOS/Probe"
        name = b"/opt/homebrew/lib/example.dylib\0"
        size = (24 + len(name) + 7) // 8 * 8
        link = struct.pack("<6I", gate.LC_LOAD_DYLIB, size, 24, 0, 0, 0)
        link += name + b"\0" * (size - 24 - len(name))
        header = struct.pack("<8I", 0xFEEDFACF, gate.ARM64, 0, 2, 2, 24 + size, 0, 0)
        build = struct.pack("<6I", gate.LC_BUILD_VERSION, 24, 1, 13 << 16, 27 << 16, 0)
        binary.write_bytes(header + build + link)
        self.assertTrue(any("build-machine dependency" in item for item in gate.check_bundle(app)["issues"]))

    def test_non_native_executable_is_not_a_zero_input_pass(self):
        app = gate.sample_bundle(self.root)
        (app / "Contents/MacOS/Probe").write_text("#!/bin/sh\n", encoding="ascii")
        self.assertTrue(gate.check_bundle(app)["issues"])

    def test_symlink_does_not_double_count_native_image(self):
        app = gate.sample_bundle(self.root)
        (app / "Contents/MacOS/Link").symlink_to("Probe")
        self.assertEqual(gate.check_bundle(app)["images"], 1)

    def test_missing_executable_and_bad_version_refuse_a_conclusion(self):
        app = gate.sample_bundle(self.root)
        info = app / "Contents/Info.plist"
        info.write_bytes(plistlib.dumps({"CFBundleExecutable": "Missing", "LSMinimumSystemVersion": "13.0"}))
        with self.assertRaises(ValueError):
            gate.check_bundle(app)
        for value in ["", "abc", "13..0", "13.0.0.0", "１３.0"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                gate.version_tuple(value)

    def test_cli_pass_failure_and_unknown_option(self):
        app = gate.sample_bundle(self.root)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(gate.main([str(app)]), 0)
            self.assertEqual(gate.main([str(self.root / "missing")]), 1)
            self.assertEqual(gate.main(["--self-test"]), 0)
            with self.assertRaises(SystemExit) as raised:
                gate.main(["--unknown"])
            self.assertEqual(raised.exception.code, 2)
            with self.assertRaises(SystemExit):
                gate.main([])

    def test_runtime_pin_and_loaded_qt_must_agree(self):
        self.assertEqual(gate.check_runtime(), [])
        with mock.patch.object(gate, "version", return_value="0.0.0"), \
             mock.patch("PyQt6.QtCore.qVersion", return_value="0.0.0"):
            self.assertEqual(len(gate.check_runtime()), 4)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(gate.main(["--runtime"]), 0)
        with mock.patch.object(gate, "REQUIREMENTS", self.root / "requirements.txt"):
            gate.REQUIREMENTS.write_text("PyQt6>=6\n", encoding="ascii")
            with self.assertRaises(ValueError):
                gate.check_runtime()
            gate.REQUIREMENTS.write_text("", encoding="ascii")
            with self.assertRaises(ValueError):
                gate.check_runtime()
