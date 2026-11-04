"""2026-11-02 走查 F-J-01：README 的下载链接与校验文件名必须与当前版本同源。

1.4.109 发版时 README 只改了版本头与变更列表，下载行停在 1.4.108，
而 public_docs_check 只查 INSTALL.md 的文件名，于是新用户点下载拿到上一版。
"""
from pathlib import Path
import sys
import tempfile

from tests._base import SFCase

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import public_docs_check as docs  # noqa: E402

OLD = "0.0.1"  # 只用来造“上一版”，不是任何真实版本


class ReadmeDownloadLinksMatchTheVersion(SFCase):
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory(prefix="sfdocs_readme_")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name in (*docs.REQUIRED, "LICENSE", "release_notes.txt"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((ROOT / name).read_bytes())
        self.readme = self.root / "README.md"
        self.original = self.readme.read_text(encoding="utf-8")

    def problems(self):
        return docs.problems(self.root, docs.APP_VERSION)

    def test_shipped_readme_download_lines_are_current(self):
        self.assertEqual(self.problems(), [])

    def test_stale_dmg_file_name_is_rejected_and_named(self):
        stale = self.original.replace("V" + docs.APP_VERSION + ".dmg", "V" + OLD + ".dmg")
        self.assertNotEqual(stale, self.original, "the injection must modify the README")
        self.readme.write_text(stale, encoding="utf-8")
        self.assertTrue(any("README.md" in item and "dmg file name" in item for item in self.problems()),
                        self.problems())

    def test_stale_download_link_is_rejected_and_named(self):
        stale = self.original.replace("version=" + docs.APP_VERSION, "version=" + OLD)
        self.assertNotEqual(stale, self.original)
        self.readme.write_text(stale, encoding="utf-8")
        self.assertTrue(any("README.md" in item and "download link" in item for item in self.problems()),
                        self.problems())

    def test_download_label_cannot_point_to_an_unrelated_old_asset(self):
        bad_link = "[Skyrocket-V%s.dmg](https://example.com/old.dmg)" % docs.APP_VERSION
        self.readme.write_text(self.original + "\n" + bad_link + "\n", encoding="utf-8")
        self.assertTrue(any("download link" in item for item in self.problems()), self.problems())

    def test_download_link_rejects_relative_and_wrong_path_destinations(self):
        for destination in ("old.dmg", "https://skyrocket-downloads.pages.dev/download-extra?version=%s&asset=Skyrocket-V%s.dmg" % (docs.APP_VERSION, docs.APP_VERSION)):
            with self.subTest(destination=destination):
                link = "[Skyrocket-V%s.dmg](%s)" % (docs.APP_VERSION, destination)
                self.readme.write_text(self.original + "\n" + link + "\n", encoding="utf-8")
                self.assertTrue(any("download link" in item for item in self.problems()), self.problems())

    def test_backup_download_link_to_old_asset_is_rejected(self):
        self.readme.write_text(self.original + "\n[备用下载](https://example.com/old.dmg)\n", encoding="utf-8")
        self.assertTrue(any("download link" in item for item in self.problems()), self.problems())

    def test_commented_official_link_does_not_validate_visible_bad_download(self):
        url = "https://skyrocket-downloads.pages.dev/download?version=%s&asset=Skyrocket-V%s.dmg" % (docs.APP_VERSION, docs.APP_VERSION)
        visible = self.original.replace(url, "https://example.com/old.dmg").replace(
            "[下载 `Skyrocket-V%s.dmg`]" % docs.APP_VERSION, "[下载]")
        self.readme.write_text(visible + "\n<!-- [下载](%s) -->\nSkyrocket-V%s.dmg\n" % (url, docs.APP_VERSION), encoding="utf-8")
        self.assertTrue(any("download link" in item for item in self.problems()), self.problems())

    def test_download_url_requires_matching_asset_parameter(self):
        asset = "&asset=Skyrocket-V" + docs.APP_VERSION + ".dmg"
        for replacement in ("", "&asset=wrong.dmg", "&asset=Skyrocket-V" + OLD + ".dmg"):
            with self.subTest(replacement=replacement):
                broken = self.original.replace(asset, replacement)
                self.assertNotEqual(broken, self.original)
                self.readme.write_text(broken, encoding="utf-8")
                self.assertTrue(any("download link" in item for item in self.problems()), self.problems())

    def test_download_query_order_is_irrelevant_and_duplicates_are_rejected(self):
        version = docs.APP_VERSION
        original_query = "version=" + version + "&asset=Skyrocket-V" + version + ".dmg"
        reordered = "asset=Skyrocket-V" + version + ".dmg&version=" + version
        self.assertIn(original_query, self.original)
        self.readme.write_text(self.original.replace(original_query, reordered), encoding="utf-8")
        self.assertEqual(self.problems(), [])
        for extra in ("&version=" + version, "&asset=Skyrocket-V" + version + ".dmg"):
            with self.subTest(extra=extra):
                self.readme.write_text(self.original.replace(original_query, original_query + extra), encoding="utf-8")
                self.assertTrue(any("download link" in item for item in self.problems()), self.problems())

    def test_stale_checksum_file_name_is_rejected_and_named(self):
        stale = self.original.replace("Skyrocket-" + docs.APP_VERSION + "-SHA256SUMS",
                                      "Skyrocket-" + OLD + "-SHA256SUMS")
        self.assertNotEqual(stale, self.original)
        self.readme.write_text(stale, encoding="utf-8")
        self.assertTrue(any("README.md" in item and "checksum file name" in item for item in self.problems()),
                        self.problems())

    def test_one_stale_token_among_current_ones_is_still_rejected(self):
        # 只改第一处：同一类 token 里混进一个旧版本也要判红（不能只看“有没有当前版本”）。
        current_dmg = "V" + docs.APP_VERSION + ".dmg"
        stale = self.original.replace(current_dmg, "V" + OLD + ".dmg", 1)
        self.assertNotEqual(stale, self.original)
        self.assertIn(current_dmg, stale)
        self.readme.write_text(stale, encoding="utf-8")
        self.assertTrue(any("dmg file name" in item for item in self.problems()), self.problems())

    def test_missing_download_lines_are_rejected(self):
        # 整段下载行被删掉（而不是写错）同样不能放行。
        lines = [line for line in self.original.splitlines()
                 if "download?version=" not in line and "SHA256SUMS" not in line]
        self.readme.write_text("\n".join(lines) + "\n", encoding="utf-8")
        found = self.problems()
        for label in ("download link", "dmg file name", "checksum file name"):
            self.assertTrue(any(label in item for item in found), (label, found))
