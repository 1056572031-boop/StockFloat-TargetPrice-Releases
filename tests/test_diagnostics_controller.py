import os
import tempfile
from unittest import main

from sf_diagnostics_controller import DiagnosticsExportController

try:
    from _base import SFCase, wait_for
except ImportError:
    from tests._base import SFCase, wait_for


class _Archive:
    def __init__(self, error=None):
        self.error = error
        self.finished = False

    def finish(self):
        if self.error is not None:
            raise self.error
        self.finished = True


class TestDiagnosticsExportController(SFCase):
    def setUp(self):
        super().setUp()
        self.results = []
        self.published = []
        self.controller = DiagnosticsExportController(
            lambda temporary, requested: self._publish(temporary, requested),
        )

    def _temporary(self):
        fd, path = tempfile.mkstemp(prefix="sf_diag_controller_")
        os.close(fd)
        return path

    def _publish(self, temporary, requested):
        self.published.append((temporary, requested))
        return "/tmp/diagnostics.zip"

    def test_start_publishes_result_and_cleans_temporary_file(self):
        temporary = self._temporary()
        archive = _Archive()
        worker = self.controller.start(
            archive, temporary, "/tmp/requested.zip", self.results.append)

        self.assertIsNotNone(worker)
        self.assertTrue(wait_for(lambda: bool(self.results)))
        self.assertTrue(archive.finished)
        self.assertEqual(self.results, [("/tmp/diagnostics.zip", None)])
        self.assertFalse(os.path.exists(temporary))
        self.assertFalse(self.controller.exporting)

    def test_failure_reports_exception_type_and_cleans_temporary_file(self):
        temporary = self._temporary()
        self.controller.start(
            _Archive(OSError("disk full")), temporary, "requested", self.results.append)

        self.assertTrue(wait_for(lambda: bool(self.results)))
        self.assertEqual(self.results, [(None, "OSError")])
        self.assertFalse(os.path.exists(temporary))

    def test_duplicate_start_is_rejected_while_exporting(self):
        archive = _Archive()
        temporary = self._temporary()
        self.controller.exporting = True
        self.assertIsNone(
            self.controller.start(archive, temporary, "requested", self.results.append))
        os.unlink(temporary)

    def test_finish_sync_uses_the_same_completion_contract(self):
        temporary = self._temporary()
        self.controller.finish_sync(_Archive(), temporary, "requested", self.results.append)

        self.assertEqual(self.results, [("/tmp/diagnostics.zip", None)])
        self.assertFalse(self.controller.exporting)


if __name__ == "__main__":
    main()
