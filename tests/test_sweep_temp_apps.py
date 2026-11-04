# -*- coding: utf-8 -*-
"""tools/sweep_temp_apps.py 回归：临时目录里"可启动的本程序旧副本"必须被改名中性化。

背景：2026-09-22 / 09-23 两份"程序意外退出"报告（均为 v1.3.97）的崩溃路径都是
/private/tmp/*/实时股价-目标价版.app —— 历史验证遗留的完整旧副本被重新拉起，
在 PyQt6 导入期 SIGSEGV。v1.4.5 的 sf_bootstrap 守卫只在新产物里，管不到旧副本，
所以必须由发版侧清扫。本文件全部是行为断言：只看目录是否还在、名字变成什么。
"""

import os
import plistlib
import sys
import tempfile
import unittest
from unittest.mock import patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "tools"))

import sweep_temp_apps as sweep  # noqa: E402

try:
    from _base import SFCase
except ImportError:
    from tests._base import SFCase


def _make_app(parent, name, bundle_id, version="1.3.97", with_binary=True):
    """造一个最小 .app 目录：Contents/Info.plist(+可选可执行文件)。"""
    app = os.path.join(parent, name)
    macos = os.path.join(app, "Contents", "MacOS")
    os.makedirs(macos, exist_ok=True)
    if with_binary:
        with open(os.path.join(macos, "StockFloatTargetPrice"), "wb") as handle:
            handle.write(b"MZ-fake")
    if bundle_id is not None:
        with open(os.path.join(app, "Contents", "Info.plist"), "wb") as handle:
            plistlib.dump({"CFBundleIdentifier": bundle_id,
                           "CFBundleShortVersionString": version}, handle)
    return app


class TestFindStaleApps(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="sf_sweep_")
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name

    def _hits(self):
        return [path for path, _reason in
                sweep.find_stale_apps([self.root], sweep.BUNDLE_ID, max_depth=6)]

    def test_own_launchable_copy_is_found(self):
        app = _make_app(self.root, "实时股价-目标价版.app", sweep.BUNDLE_ID)
        self.assertEqual(self._hits(), [app])

    def test_foreign_app_is_ignored(self):
        _make_app(self.root, "别的应用.app", "com.example.other")
        self.assertEqual(self._hits(), [])

    def test_copy_without_executable_is_ignored(self):
        # 测试夹具造的空 .app 目录不会被系统当应用，没必要改名。
        _make_app(self.root, "实时股价-目标价版.app", sweep.BUNDLE_ID,
                  with_binary=False)
        self.assertEqual(self._hits(), [])

    def test_copy_missing_info_plist_is_found_by_name(self):
        # 临时目录被系统回收时常常先丢 Info.plist，这类"半具尸体"正是崩溃来源。
        app = _make_app(self.root, "实时股价-目标价版.app", None)
        self.assertEqual(self._hits(), [app])

    def test_already_neutralized_copy_is_not_relisted(self):
        app = _make_app(self.root, "实时股价-目标价版.app", sweep.BUNDLE_ID)
        sweep.neutralize(app)
        self.assertEqual(self._hits(), [])


class TestNeutralize(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="sf_sweep_")
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name

    def test_renames_without_deleting(self):
        app = _make_app(self.root, "实时股价-目标价版.app", sweep.BUNDLE_ID)
        new = sweep.neutralize(app)
        self.assertFalse(os.path.exists(app))
        self.assertTrue(os.path.isdir(new))
        self.assertTrue(new.endswith(".app.stale"))
        # 内容仍在：去掉后缀即可还原
        self.assertTrue(os.path.isfile(
            os.path.join(new, "Contents", "MacOS", "StockFloatTargetPrice")))

    def test_collision_appends_index(self):
        app = _make_app(self.root, "实时股价-目标价版.app", sweep.BUNDLE_ID)
        sweep.neutralize(app)
        again = _make_app(self.root, "实时股价-目标价版.app", sweep.BUNDLE_ID)
        second = sweep.neutralize(again)
        self.assertTrue(second.endswith(".app.stale.1"), second)


class TestCliBehaviour(unittest.TestCase):
    """main() 的 --apply / 预览语义：默认不动盘，加 --apply 才改名。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="sf_sweep_")
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        self.app = _make_app(self.root, "实时股价-目标价版.app", sweep.BUNDLE_ID)
        self.patch_roots = patch.object(sweep, "temp_roots", lambda: [self.root])
        self.patch_running = patch.object(sweep, "running_bundle_paths",
                                          lambda: set())
        self.patch_roots.start()
        self.patch_running.start()
        self.addCleanup(self.patch_roots.stop)
        self.addCleanup(self.patch_running.stop)

    def test_dry_run_changes_nothing(self):
        self.assertEqual(sweep.main([]), 0)
        self.assertTrue(os.path.isdir(self.app), "预览模式不应改名")

    def test_apply_renames(self):
        self.assertEqual(sweep.main(["--apply"]), 0)
        self.assertFalse(os.path.exists(self.app))
        self.assertTrue(os.path.isdir(self.app + ".stale"))

    def test_running_copy_is_skipped(self):
        with patch.object(sweep, "running_bundle_paths",
                          lambda: {os.path.realpath(self.app)}):
            self.assertEqual(sweep.main(["--apply"]), 0)
        self.assertTrue(os.path.isdir(self.app), "正在运行的副本不得被改名")


class TestScanScope(unittest.TestCase):
    def test_roots_are_temp_only(self):
        roots = sweep.temp_roots()
        self.assertTrue(roots, "应至少识别出一个临时目录")
        for root in roots:
            self.assertTrue(
                root.startswith(("/private/tmp", "/private/var")),
                "扫描根必须限定在系统临时目录，实际: %s" % root)


if __name__ == "__main__":
    unittest.main()


class TestMalformedInfoPlistDoesNotAbortTheSweep(SFCase):
    """2026-11-02 走查 F-C-03：plistlib 对 XML 语法错误抛 ExpatError（既不是 ValueError
    也不是 OSError 的子类），旧写法没接住 —— 临时目录里只要躺着一份第三方 app 的截断
    Info.plist，整轮清扫（含本程序旧副本）就在 find_stale_apps 里中止、什么都没改名。
    """

    TRUNCATED = (b'<?xml version="1.0"?><plist version="1.0"><dict>'
                 b'<key>CFBundleIdentifier</key>')

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory(prefix="sf_sweep_bad_")
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name

    def _broken_third_party(self, parent="a_broken"):
        app = _make_app(os.path.join(self.root, parent), "Broken.app", None)
        with open(os.path.join(app, "Contents", "Info.plist"), "wb") as handle:
            handle.write(self.TRUNCATED)
        return app

    def test_truncated_xml_plist_reads_as_missing(self):
        import sf_temp_sweep
        app = self._broken_third_party()
        self.assertIsNone(sf_temp_sweep.bundle_identifier(app))
        self.assertEqual(sf_temp_sweep.version_of(app), "?")

    def test_sweep_still_neutralizes_own_copy_next_to_a_broken_third_party_app(self):
        import sf_temp_sweep
        broken = self._broken_third_party("a_broken")
        own = _make_app(os.path.join(self.root, "z_own"), "实时股价-目标价版.app",
                        sf_temp_sweep.BUNDLE_ID)
        with patch.object(sf_temp_sweep, "running_bundle_paths", return_value=set()):
            changed = sf_temp_sweep.sweep_temporary_copies([self.root], log=lambda _m: None)
        self.assertEqual(changed, 1, "畸形 plist 不得让整轮清扫中止")
        self.assertTrue(os.path.isdir(own + ".stale"), "本程序旧副本应被改名中性化")
        self.assertTrue(os.path.isdir(broken), "第三方 app 一个字都不许动（边界 2）")

    def test_own_copy_with_broken_plist_falls_back_to_name_recognition(self):
        """自家副本的 plist 本身被截断：等同 Info.plist 读不到，走按包名识别。"""
        import sf_temp_sweep
        own = _make_app(os.path.join(self.root, "x"), "实时股价-目标价版.app", None)
        with open(os.path.join(own, "Contents", "Info.plist"), "wb") as handle:
            handle.write(self.TRUNCATED)
        hits = sf_temp_sweep.find_stale_apps([self.root])
        self.assertEqual([path for path, _reason in hits], [own])

    def test_unexpected_error_on_one_app_is_logged_and_skipped(self):
        """单个 .app 上任何意外异常都只跳过这一份并留痕，其余副本照常处理。"""
        import sf_temp_sweep
        bad = _make_app(os.path.join(self.root, "a_bad"), "Bad.app", sf_temp_sweep.BUNDLE_ID)
        own = _make_app(os.path.join(self.root, "z_own"), "实时股价-目标价版.app",
                        sf_temp_sweep.BUNDLE_ID)
        real = sf_temp_sweep.bundle_identifier

        def flaky(app_dir):
            if app_dir == bad:
                raise RuntimeError("boom on one app")
            return real(app_dir)

        notes = []
        with patch.object(sf_temp_sweep, "bundle_identifier", side_effect=flaky):
            hits = sf_temp_sweep.find_stale_apps([self.root], log=notes.append)
        self.assertEqual([path for path, _reason in hits], [own])
        self.assertTrue(any("Bad.app" in n for n in notes), "跳过的那份必须留痕：%r" % (notes,))
