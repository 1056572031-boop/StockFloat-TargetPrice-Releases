import os
import tempfile
import threading
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


class _BlockingArchive(_Archive):
    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def finish(self):
        self.entered.set()
        self.release.wait(2.0)
        super().finish()


class _ConcurrentStartController(DiagnosticsExportController):
    def __init__(self, publish):
        super().__init__(publish)
        self._exporting_writes = threading.Barrier(2)

    @property
    def exporting(self):
        return DiagnosticsExportController.exporting.fget(self)

    @exporting.setter
    def exporting(self, value):
        if value:
            try:
                self._exporting_writes.wait(timeout=1.0)
            except threading.BrokenBarrierError:
                pass
        DiagnosticsExportController.exporting.fset(self, value)


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

    def test_finish_sync_is_rejected_after_close(self):
        temporary = self._temporary()
        self.controller.close()

        self.controller.finish_sync(_Archive(), temporary, "requested", self.results.append)

        self.assertEqual(self.results, [])
        self.assertEqual(self.published, [])
        self.assertTrue(os.path.exists(temporary))
        os.unlink(temporary)

    def test_concurrent_start_accepts_only_one_export(self):
        """两个入口同时请求时，只允许一个诊断导出占用控制器。"""
        controller = _ConcurrentStartController(
            lambda temporary, requested: self._publish(temporary, requested),
        )
        for _ in range(200):
            archives = [_BlockingArchive(), _BlockingArchive()]
            temporary = [self._temporary(), self._temporary()]
            workers = []
            barrier = threading.Barrier(2)

            def start_export(index):
                barrier.wait()
                worker = controller.start(
                    archives[index], temporary[index], "requested", self.results.append)
                if worker is not None:
                    workers.append(worker)

            threads = [threading.Thread(target=start_export, args=(index,)) for index in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertLessEqual(len(workers), 1)
            for archive in archives:
                archive.release.set()
            for worker in workers:
                worker.join(1.0)
            for path in temporary:
                if os.path.exists(path):
                    os.unlink(path)

            if self.results:
                self.results.clear()

    def test_close_during_publish_drops_completion_callback(self):
        temporary = self._temporary()
        archive = _Archive()

        def publish_and_close(path, requested):
            self.controller.close()
            self.published.append((path, requested))
            return "/tmp/diagnostics.zip"

        self.controller._publish = publish_and_close  # discipline-ok(R5): 注入发布期间关闭的竞态
        self.controller.finish_sync(archive, temporary, "requested", self.results.append)

        self.assertEqual(self.published, [(temporary, "requested")])
        self.assertEqual(self.results, [])
        self.assertFalse(os.path.exists(temporary))

    def test_finish_sync_uses_the_same_completion_contract(self):
        temporary = self._temporary()
        self.controller.finish_sync(_Archive(), temporary, "requested", self.results.append)

        self.assertEqual(self.results, [("/tmp/diagnostics.zip", None)])
        self.assertFalse(self.controller.exporting)

    def test_close_during_blocked_export_drops_publish_and_callback(self):
        temporary = self._temporary()
        archive = _BlockingArchive()
        worker = self.controller.start(
            archive, temporary, "requested", self.results.append)

        self.assertIsNotNone(worker)
        self.assertTrue(archive.entered.wait(1.0))
        self.controller.close(timeout=0.05)
        archive.release.set()
        self.assertTrue(wait_for(lambda: not worker.is_alive()))

        self.assertEqual(self.published, [])
        self.assertEqual(self.results, [])
        self.assertFalse(self.controller.exporting)


if __name__ == "__main__":
    main()
