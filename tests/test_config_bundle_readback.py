"""包内模块加载器的离线测试；不执行真实应用。"""
import contextlib
import io
import importlib
import os
import runpy
import types
from pathlib import Path
from unittest import mock

from tests._base import SFCase
from tools import config_bundle_readback as bundle


class BundleConfigReadbackCase(SFCase):
    def test_probe_reads_archived_modules_and_removes_loader_on_failure(self):
        import tempfile
        with tempfile.TemporaryDirectory(prefix="sf_bundle_probe_") as temp:
            config = Path(temp) / "config.json"
            config.write_text("{}")
            modules = types.SimpleNamespace(modules={}, meta_path=[])
            reader = mock.Mock(toc={"PYZ.pyz": None})
            reader.extract.return_value = b"fixture archive"
            config_module = mock.Mock()
            config_module.load_config.return_value = ("fixture", 120, 120, 320, "glass")
            config_module.load_alerts.return_value = []
            constants = types.SimpleNamespace(APP_VERSION="fixture")
            with mock.patch.object(bundle, "sys", modules), mock.patch.dict(os.environ), \
                    mock.patch.object(bundle, "bundle_executable", return_value=Path(temp) / "executable"), \
                    mock.patch.object(bundle, "CArchiveReader", return_value=reader), \
                    mock.patch.object(bundle, "ZlibArchiveReader"), \
                    mock.patch.object(importlib, "import_module", side_effect=[config_module, constants]):
                value = bundle.probe(Path(temp) / "app", config)
            self.assertEqual(value["bundle_version"], "fixture")
            self.assertEqual(value["window"][0], "fixture")
            self.assertEqual(modules.meta_path, [])
            reader.extract.assert_called_once_with("PYZ.pyz")
            with mock.patch.object(bundle, "sys", modules), mock.patch.dict(os.environ), \
                    mock.patch.object(bundle, "bundle_executable", return_value=Path(temp) / "executable"), \
                    mock.patch.object(bundle, "CArchiveReader", return_value=reader), \
                    mock.patch.object(bundle, "ZlibArchiveReader"), \
                    mock.patch.object(importlib, "import_module", side_effect=ImportError("missing archive module")), \
                    self.assertRaises(ImportError):
                bundle.probe(Path(temp) / "app", config)
            self.assertEqual(modules.meta_path, [], "failure must not leak a process-wide import loader")

    def test_loader_executes_archive_code_and_never_falls_back_for_missing_module(self):
        archive = mock.Mock(toc={"sf_fixture": None})
        archive.extract.return_value = compile("value = 'from bundle'", "<sf-bundle-fixture>", "exec")
        loader = bundle.BundleModules(archive)
        spec = loader.find_spec("sf_fixture")
        self.assertIs(spec.loader, loader)
        self.assertIsNone(loader.create_module(spec))
        module = types.ModuleType("sf_fixture")
        loader.exec_module(module)
        self.assertEqual(module.value, "from bundle")
        archive.extract.assert_called_once_with("sf_fixture")
        with self.assertRaisesRegex(ImportError, "拒绝回退"):
            loader.find_spec("sf_not_in_bundle")
        self.assertIsNone(loader.find_spec("json"))

    def test_probe_rejects_non_private_config_or_preloaded_product(self):
        with self.assertRaisesRegex(ValueError, "私有临时"):
            bundle.probe(Path("unused.app"), Path("/Applications/user-config.json"))
        import tempfile
        with tempfile.TemporaryDirectory(prefix="sf_bundle_config_") as temp:
            config = Path(temp) / "config.json"
            config.write_text("{}")
            with mock.patch.dict(bundle.sys.modules, {"sf_fixture_preloaded": types.ModuleType("sf_fixture_preloaded")}), \
                    self.assertRaisesRegex(ValueError, "新进程"):
                bundle.probe(Path("unused.app"), config)

    def test_cli_dispatches_only_explicit_app_and_config(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            bundle.main([])
        self.assertEqual(raised.exception.code, 2)
        with mock.patch.object(bundle, "probe", return_value={"bundle_version": "fixture"}) as probe, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(bundle.main(["--app", "candidate.app", "--config", "config.json"]), 0)
        probe.assert_called_once_with(Path("candidate.app"), Path("config.json"))
        with mock.patch.object(bundle.sys, "argv", [bundle.__file__]), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            runpy.run_path(bundle.__file__, run_name="__main__")
        self.assertEqual(raised.exception.code, 2)
