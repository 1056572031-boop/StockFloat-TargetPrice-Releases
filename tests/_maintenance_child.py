"""Exercise both nested blocks through compiled Objective-C, without OS deferral."""
import argparse
import ctypes
import pathlib
import shutil
import subprocess
import sys
import tempfile
import types
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

NATIVE_SOURCE = r"""
#import <Foundation/Foundation.h>
static NSInteger completed = 0;
@interface SFNativeMaintenanceProbe : NSBackgroundActivityScheduler
@end
@implementation SFNativeMaintenanceProbe
- (void)scheduleWithBlock:(void (^)(NSBackgroundActivityCompletionHandler))block {
    block(^(NSBackgroundActivityResult result) { completed = result; });
}
- (void)invalidate {}
@end
NSInteger SFNativeMaintenanceResult(void) { return completed; }
"""


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=pathlib.Path)
    parser.add_argument("--unpinned", action="store_true")
    args = parser.parse_args(argv)
    clang = shutil.which("clang")
    if sys.platform != "darwin" or not clang:
        print("Native maintenance probe requires macOS and the Clang toolchain")
        return 77
    import Foundation
    import AppKit  # noqa: F401  Match the framework imports of a real Cocoa launch.
    import objc
    if args.app is not None:
        from tools.verify_dmg_pyz import Bundle
        bundle = Bundle(args.app)
        try:
            code = bundle.code("sf_maintenance")
            if code is None:
                raise RuntimeError("packaged maintenance module is missing")
            module = types.ModuleType("sf_maintenance")
            exec(code, module.__dict__)
            sys.modules["sf_maintenance"] = module
            print("TESTED_INSTALLED_VERSION=%s" % bundle.version, flush=True)
        finally:
            bundle.dispose()
    import sf_maintenance
    from sf_maintenance import BackgroundMaintenance

    if args.unpinned:
        sf_maintenance._pin_completion_block_signature = lambda: None  # discipline-ok(R5): deliberately remove the native bridge fix in a separate process

    with tempfile.TemporaryDirectory(prefix="sf_maintenance_") as directory:
        library = pathlib.Path(directory) / "probe.dylib"
        compiled = subprocess.run(
            [clang, "-x", "objective-c", "-dynamiclib", "-fblocks", "-framework",
             "Foundation", "-o", str(library), "-"], input=NATIVE_SOURCE,
            capture_output=True, text=True, timeout=20)
        if compiled.returncode:
            print(compiled.stderr)
            return 1
        native = ctypes.CDLL(str(library))
        native.SFNativeMaintenanceResult.restype = ctypes.c_long
        probe = objc.lookUpClass("SFNativeMaintenanceProbe")
        work = []
        with mock.patch.object(Foundation, "NSBackgroundActivityScheduler", probe):
            maintenance = BackgroundMaintenance(21600)
            if not maintenance.start(lambda: work.append(True)):
                print("Native scheduler setup failed", maintenance.diagnostics())
                print("NATIVE_COMPLETION=%d" % native.SFNativeMaintenanceResult())
                print("WORK_CALLS=%d" % len(work))
                return 1
        maintenance.stop()
        result = native.SFNativeMaintenanceResult()
        print("NATIVE_COMPLETION=%d" % result)
        print("WORK_CALLS=%d" % len(work))
        return 0 if result == 1 and work == [True] and not maintenance.failure_count else 1


if __name__ == "__main__":
    raise SystemExit(main())
