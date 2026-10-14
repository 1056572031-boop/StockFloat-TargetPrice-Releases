"""Execute the packaging recipe with a small Analysis fixture, without building."""

from pathlib import Path
import runpy
from types import SimpleNamespace
from unittest.mock import mock_open, patch

from tests._base import SFCase


ROOT = Path(__file__).resolve().parents[1]


def package_resources(datas, binaries=()):
    analysis = SimpleNamespace(datas=list(datas), binaries=list(binaries),
                               pure=[], scripts=[])
    collected = {}

    def analyze(*args, **kwargs):
        collected["requested_datas"] = kwargs["datas"]
        return analysis

    def collect(exe, packaged_binaries, packaged_datas, **kwargs):
        collected.update(datas=packaged_datas, binaries=packaged_binaries)
        return SimpleNamespace(**kwargs)

    constructors = {
        "Analysis": analyze,
        "PYZ": lambda *args, **kwargs: object(),
        "EXE": lambda *args, **kwargs: object(),
        "COLLECT": collect,
        "BUNDLE": lambda *args, **kwargs: object(),
    }
    constants = (ROOT / "sf_constants.py").read_text(encoding="utf-8")
    with patch("builtins.open", mock_open(read_data=constants)):
        runpy.run_path(str(ROOT / "StockFloat.spec"), init_globals=constructors)
    return collected


def resource(destination, kind="DATA"):
    return destination, "/fixture/" + Path(destination).name, kind


class TestBundleResources(SFCase):
    def test_project_docs_and_titlecase_dependency_license_are_collected(self):
        dependency = SimpleNamespace(files=[Path("licenses/License.txt"), Path("COPYING"),
                                            Path("package/not-a-license.py")],
                                     locate_file=lambda entry: Path("/dependency") / entry)
        with patch("importlib.metadata.distribution", return_value=dependency):
            requested = package_resources([])["requested_datas"]
        for entry in (("README.md", "docs"), ("SECURITY.md", "docs"),
                      ("docs/INSTALL.md", "docs"), ("LICENSE", "licenses")):
            self.assertIn(entry, requested)
        for package in ("pyobjc-core", "pyobjc-framework-UserNotifications", "pyobjc-framework-AVFoundation",
                        "pyobjc-framework-CoreMedia", "pyobjc-framework-CoreAudio"):
            self.assertIn(("/dependency/licenses/License.txt", "licenses/" + package), requested)
            self.assertIn(("/dependency/COPYING", "licenses/" + package), requested)
        self.assertFalse(any("not-a-license" in source for source, destination in requested))

    def test_keeps_chinese_and_english_for_all_qt_translation_catalogs(self):
        entries = [resource("PyQt6/Qt6/translations/%s_%s.qm" % (catalog, locale))
                   for catalog in ("qtbase", "qt", "qt_help")
                   for locale in ("zh_CN", "zh_TW", "en")]
        self.assertEqual(package_resources(entries)["datas"], entries)

    def test_removes_other_qt_translation_languages(self):
        entries = [resource("PyQt6/Qt6/translations/qtbase_%s.qm" % locale)
                   for locale in ("fr", "de", "ja", "ko", "pt_BR", "ru")]
        self.assertEqual(package_resources(entries)["datas"], [])

    def test_preserves_non_translation_assets_and_unrelated_catalogs(self):
        entries = [resource(name) for name in (
            "fonts/D-DIN.otf", "fonts/OFL.txt", "updater/update_helper.sh",
            "icon.icns", "PyQt6/Qt6/translations/LICENSE.txt",
            "translations/qtbase_fr.qm", "Other/Qt6/translations/qtbase_fr.qm",
            "PyQt6/Qt6/translations-extra/qtbase_fr.qm",
        )]
        self.assertEqual(package_resources(entries)["datas"], entries)

    def test_preserves_runtime_libraries_and_all_image_plugins(self):
        entries = [resource(name, "BINARY") for name in (
            "libpython3.14.dylib", "_cffi_backend.cpython-314-darwin.so",
            "nacl/_sodium.abi3.so", "AVFoundation/_AVFoundation.so",
            "UserNotifications/_UserNotifications.so",
            "PyQt6/Qt6/lib/QtDBus.framework/Versions/A/QtDBus",
            "PyQt6/Qt6/lib/QtSvg.framework/Versions/A/QtSvg",
            "PyQt6/Qt6/plugins/platforms/libqcocoa.dylib",
            "PyQt6/Qt6/plugins/styles/libqmacstyle.dylib",
        )]
        entries += [resource("PyQt6/Qt6/plugins/imageformats/libq%s.dylib" % fmt,
                             "BINARY")
                    for fmt in ("jpeg", "webp", "tiff", "gif", "ico", "icns", "svg",
                                "macheif", "macjp2", "wbmp", "tga")]
        self.assertEqual(package_resources([], entries)["binaries"], entries)

    def test_existing_pdf_network_exclusions_still_apply(self):
        entries = [resource(name, "BINARY") for name in (
            "PyQt6/Qt6/lib/QtPdf.framework/Versions/A/QtPdf",
            "PyQt6/Qt6/lib/QtNetwork.framework/Versions/A/QtNetwork",
            "PyQt6/Qt6/plugins/imageformats/libqpdf.dylib",
            "PyQt6/Qt6/plugins/generic/libqtuiotouchplugin.dylib",
        )]
        data = [resource(name) for name in (
            "PyQt6/Qt6/lib/QtPdf.framework/Resources/Info.plist",
            "PyQt6/Qt6/lib/QtNetwork.framework/Resources/Info.plist",
        )]
        packaged = package_resources(data, entries)
        self.assertEqual(packaged["datas"], [])
        self.assertEqual(packaged["binaries"], [])
