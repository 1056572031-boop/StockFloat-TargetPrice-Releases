"""GUI snapshots stay on the GUI thread while file collection can be slow."""
from pathlib import Path
import threading
from unittest import mock
import zipfile

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication
import sf_diagnostics
from tests._base import SFCase, wait_for
from tests.test_coverage_window_diagnostics import DiagnosticsBranchCase


def member(owner, name):
    return getattr(owner, name)


class AsyncDiagnosticsQualityTests(SFCase):
    def fixture(self):
        case = DiagnosticsBranchCase()
        case.setUp()
        self.addCleanup(case.doCleanups)
        self.addCleanup(case.tearDown)
        return case

    def test_slow_file_collection_does_not_block_gui_or_start_duplicate_exports(self):
        case = self.fixture()
        entered, release = threading.Event(), threading.Event()
        collect = sf_diagnostics.write_attachments

        def slow(*args):
            entered.set()
            release.wait(3)
            return collect(*args)

        with mock.patch.object(sf_diagnostics, "write_attachments", side_effect=slow):
            case.win._export_diagnostics(background=True)  # discipline-ok(R5): production menu action in a fully isolated real-widget fixture
            worker = member(case.win, "_diagnostics_thread")
            try:
                self.assertTrue(entered.wait(2))
                callbacks = []
                QTimer.singleShot(0, lambda: callbacks.append(True))
                QApplication.processEvents()
                self.assertEqual(callbacks, [True])
                case.win._export_diagnostics(background=True)  # discipline-ok(R5): repeat click must not create another archive or worker
                self.assertIs(member(case.win, "_diagnostics_thread"), worker)
            finally:
                release.set()
                worker.join(5)
        self.assertFalse(worker.is_alive())
        def completed():
            QApplication.processEvents()
            return not member(case.win, "_diagnostics_exporting")
        self.assertTrue(wait_for(completed))
        desktop = Path(case.home) / "Desktop"
        files = list(desktop.glob("*.zip"))
        self.assertEqual(len(files), 1)
        self.assertEqual(list(desktop.iterdir()), files)
        with zipfile.ZipFile(files[0]) as archive:
            self.assertIn("export.json", archive.namelist())
            self.assertIn("diagnostics.txt", archive.namelist())

    def test_background_failure_leaves_no_visible_or_partial_archive(self):
        case = self.fixture()
        with mock.patch.object(sf_diagnostics.DiagnosticArchive, "finish", side_effect=OSError("disk full")) as finish:
            case.win._export_diagnostics(background=True)  # discipline-ok(R5): exercise the actual threaded error path, not a mocked completion
            worker = member(case.win, "_diagnostics_thread")
            if worker is not None:
                worker.join(5)

            def completed():
                QApplication.processEvents()
                return not member(case.win, "_diagnostics_exporting")

            self.assertTrue(wait_for(completed))
            finish.assert_called_once()
        self.assertEqual(list((Path(case.home) / "Desktop").iterdir()), [])
