"""Desktop delivery is after qualification, including prepared publication."""
import os
import subprocess
import tempfile
from pathlib import Path

from tests._base import SFCase

ROOT = Path(__file__).resolve().parents[1]
BEGIN = "# BEGIN publish_desktop_dmg"
END = "# END publish_desktop_dmg"
CALL = 'publish_desktop_dmg "$DMG_OUT" "$DESKTOP_DMG"'
QUALIFY = 'tools/quality_evidence.py qualify "$QUALITY_RECORD"'


def check_order(source):
    qualified = source.index(QUALIFY)
    prepare_exit = source.index('  exit 0', source.index('== 仅制作候选产物'))
    if min(source.index(BEGIN), source.index(CALL)) <= max(qualified, prepare_exit):
        raise ValueError("desktop write precedes prepare exit or qualification")
    for mutation in ('mkdir -p "$DESKTOP_OUTPUT_DIR"',
                     'cp -f "$DMG_OUT" "$DESKTOP_DMG"',
                     'for old in "$DESKTOP_OUTPUT_DIR"/Skyrocket-V*.dmg'):
        if mutation in source[:qualified]:
            raise ValueError("desktop write in candidate-only phase")


class ReleaseDesktopStaging(SFCase):
    def setUp(self):
        super().setUp()
        self.source = (ROOT / "build_release.sh").read_text()
        self.function = self.source.split(BEGIN, 1)[1].split(END, 1)[0]
        self.temp = tempfile.TemporaryDirectory(prefix="sf_desktop_delivery_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.desktop = self.root / "Desktop" / "Skyrocket"
        self.desktop.mkdir(parents=True)
        (self.root / ".Trash").mkdir()
        self.archive = self.root / "Skyrocket-V9.9.9.dmg"
        self.archive.write_bytes(b"pinned candidate bytes")
        self.destination = self.desktop / self.archive.name

    def invoke(self):
        env = dict(os.environ, HOME=str(self.root))
        return subprocess.run(["/bin/bash", "-c", "set -euo pipefail\n" + self.function
                               + '\npublish_desktop_dmg "$1" "$2"', "--",
                               str(self.archive), str(self.destination)], env=env,
                              capture_output=True, text=True, timeout=10)

    def test_prepared_and_normal_modes_share_delivery_after_qualification(self):
        # This check guards irreversible shell call order; executing the whole
        # production publisher would install/upload instead of testing a fixture.
        self.assertIn('QUALITY_RECORD="${SF_QUALITY_RECORD:-}"', self.source)
        check_order(self.source)
        self.assertEqual(self.source.count(CALL), 1)
        for broken in (CALL + "\n" + self.source.replace(CALL, ""),
                       self.source.replace(CALL, ""),
                       self.source.replace(QUALIFY, 'mkdir -p "$DESKTOP_OUTPUT_DIR"\n' + QUALIFY)):
            with self.subTest(mutation=broken[:60]), self.assertRaises(ValueError):
                check_order(broken)

    def test_release_state_guards_precede_build_and_irreversible_writes(self):
        # The observable contract here is irreversible shell call order, not Python source shape.
        source_qualified = 'tools/release_state.py qualify-source --state "$RELEASE_STATE"'
        begin_publish = 'tools/release_state.py begin-publish --state "$RELEASE_STATE"'
        prepared_check = 'tools/release_state.py validate --state "$RELEASE_STATE" --stage candidate_ready'
        self.assertLess(self.source.index(source_qualified), self.source.index('hdiutil create'))
        self.assertLess(self.source.index(prepared_check), self.source.index('hdiutil create'))
        self.assertLess(self.source.index(QUALIFY), self.source.index(begin_publish))
        self.assertLess(self.source.index(begin_publish), self.source.index(CALL))
        self.assertLess(self.source.index(begin_publish), self.source.index('tools/publish_update_release.py --assets'))
        self.assertGreater(self.source.index('tools/release_state.py candidate --state'),
                           self.source.index('UPDATE_JSON="$UPDATE_ASSET_DIR/Skyrocket-update.json"'))
        self.assertIn('trap release_summary EXIT', self.source)
        self.assertIn('exit "$result"', self.source)

    def test_history_audit_and_its_failure_wait_precede_build_or_desktop_mutations(self):
        # The object under test is irreversible command order, not shell prose.
        audit = 'tools/public_release.py --audit-public-history'
        wait = 'wait "$PRECHECK_HISTORY" || PRECHECK_FAILED=1'
        stop = 'if [ "$PRECHECK_FAILED" -ne 0 ]; then'
        build = '# ---- 0) 回归关卡'
        self.assertLess(self.source.index(audit), self.source.index(wait))
        self.assertLess(self.source.index(wait), self.source.index(stop))
        self.assertLess(self.source.index(stop), self.source.index(build))
        self.assertLess(self.source.index(build), self.source.index(CALL))
        self.assertIn('if [ "$LOCAL_ONLY" -eq 0 ]; then', self.source[:self.source.index(audit)])

    def test_public_export_audit_runs_and_is_waited_before_prepare_only_can_build(self):
        # 公开树审计此前只在发布阶段才发现私密内容；候选已经冻结才失败，必须在所有模式的前置并行组里。
        audit = '"$VENV_PY" tools/public_release.py >"$PRECHECK_DIR/public.log" 2>&1 &'
        pid = 'PRECHECK_PUBLIC=$!'
        wait = 'wait "$PRECHECK_PUBLIC" || PRECHECK_FAILED=1'
        stop = 'if [ "$PRECHECK_FAILED" -ne 0 ]; then'
        build = '# ---- 0) 回归关卡'
        self.assertLess(self.source.index(audit), self.source.index(pid))
        self.assertLess(self.source.index(pid), self.source.index(wait))
        self.assertLess(self.source.index(wait), self.source.index(stop))
        self.assertLess(self.source.index(stop), self.source.index(build))
        self.assertLess(self.source.index(build), self.source.index('== 仅制作候选产物'))

    def test_copies_exact_bytes_then_retires_only_own_old_dmg_names(self):
        old = self.desktop / "Skyrocket-V0.0.1.dmg"
        legacy = self.desktop / "实时股价-目标价版-V0.0.1.dmg"
        unrelated = self.desktop / "other-product.dmg"
        for path in (old, legacy, unrelated):
            path.write_bytes(path.name.encode())
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.destination.read_bytes(), self.archive.read_bytes())
        self.assertFalse(old.exists())
        self.assertFalse(legacy.exists())
        self.assertTrue(unrelated.exists())
        self.assertEqual({p.name for p in (self.root / ".Trash").iterdir()}, {old.name, legacy.name})

    def test_failed_copy_does_not_retire_existing_desktop_dmg(self):
        old = self.desktop / "Skyrocket-V0.0.1.dmg"
        old.write_bytes(b"keep old delivery")
        self.archive.unlink()
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(old.read_bytes(), b"keep old delivery")
        self.assertFalse(self.destination.exists())
        self.assertEqual(list((self.root / ".Trash").iterdir()), [])

    def test_creates_destination_directory_without_requiring_build_phase_desktop_write(self):
        self.desktop.rmdir()
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.destination.read_bytes(), self.archive.read_bytes())
