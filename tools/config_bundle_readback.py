"""在独立进程读取应用包内的配置解析代码，不启动窗口，不回退到工作区实现。"""
from __future__ import annotations

import argparse
import importlib.abc
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader

sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify_bundle_consts import bundle_executable, load_module


class BundleModules(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def __init__(self, archive):
        self.archive = archive

    def find_spec(self, fullname, path=None, target=None):
        if not fullname.startswith("sf_"):
            return None
        if fullname not in self.archive.toc:
            raise ImportError("应用包缺少模块，拒绝回退到源码: " + fullname)
        return importlib.util.spec_from_loader(fullname, self)

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        module.__file__ = "<candidate-pyz>/" + module.__name__ + ".py"
        exec(load_module(self.archive, module.__name__), module.__dict__)


def probe(app: Path, config: Path) -> dict:
    home = config.parent.resolve()
    if (not home.is_relative_to(Path(tempfile.gettempdir()).resolve()) or config.is_symlink()
            or not config.is_file()):
        raise ValueError("只允许读取私有临时目录中的普通配置文件")
    if any(name.startswith("sf_") for name in sys.modules):
        raise ValueError("必须使用未加载产品模块的新进程")
    os.environ.update(HOME=str(home), SF_CONFIG_PATH=str(config.resolve()),
                      SF_LOG_PATH=str(home / "bundle-readback.log"), SF_SINGLE_SOCK=str(home / "readback.sock"),
                      SF_UNIVERSE_PATH=str(home / "securities.json"), SF_LAST_QUOTE_PATH=str(home / "last_quote.json"))
    reader = CArchiveReader(str(bundle_executable(app)))
    name = next(name for name in reader.toc if name.startswith("PYZ"))
    with tempfile.TemporaryDirectory(prefix="sf_config_pyz_") as temp:
        pyz = Path(temp) / "bundle.pyz"
        pyz.write_bytes(reader.extract(name))
        loader = BundleModules(ZlibArchiveReader(str(pyz)))
        sys.meta_path.insert(0, loader)
        try:
            import importlib
            config_module = importlib.import_module("sf_config")
            constants = importlib.import_module("sf_constants")
            return {"window": config_module.load_config(), "alerts": config_module.load_alerts("sh600000"),
                    "bundle_version": constants.APP_VERSION}
        finally:
            sys.meta_path.remove(loader)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    print(json.dumps(probe(args.app, args.config), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
