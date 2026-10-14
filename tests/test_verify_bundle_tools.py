import importlib.util
import pathlib
import plistlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestBundleExecutable(unittest.TestCase):
    def test_tools_read_cf_bundle_executable(self):
        for name in ("verify_dmg_pyz", "verify_bundle_consts"):
            tool = _load(name)
            with tempfile.TemporaryDirectory() as tmp:
                app = pathlib.Path(tmp) / "Custom.app"
                (app / "Contents" / "MacOS").mkdir(parents=True)
                with (app / "Contents" / "Info.plist").open("wb") as f:
                    plistlib.dump({"CFBundleExecutable": "CustomExec"}, f)
                self.assertEqual(tool.bundle_executable(app),
                                 app / "Contents" / "MacOS" / "CustomExec")

    def test_historical_version_checkers_are_not_active_tools(self):
        active = {p.name for p in (ROOT / "tools").glob("verify_v*.py")}
        self.assertFalse(active, "旧版本专项脚本不得混入当前发布工具目录")
        archive = ROOT / "tools" / "archive"
        if not archive.is_dir():
            # 公开快照有意排除 tools/archive（历史专项脚本不外发），所以这条如实跳过
            # 并打出理由：不能让公开仓库自带一条永远红的用例（那会把真的红淹没），
            # 也不删下面的归档完整性断言 —— 私有仓仍然要守"历史脚本一个都不能丢"。
            self.skipTest("本副本不含 tools/archive（公开快照有意排除），"
                          "归档完整性只在私有仓核验")
        archived = {p.name for p in archive.glob("verify_v*.py")}
        expected = {"verify_v1287.py", "verify_v139.py", "verify_v1310.py",
                    "verify_v1311.py", "verify_v1313.py", "verify_v1314.py",
                    "verify_v1345.py", "verify_v1346.py",
                    "verify_v1348.py", "verify_v1350.py",
                    "verify_v1352.py"}
        self.assertEqual(archived, expected,
                         "历史专项脚本必须完整归档，避免丢失交付追溯依据")


class TestDmgPyzChecks(unittest.TestCase):
    """verify_dmg_pyz 的判定逻辑：结论必须与实际跑过的检查一致。

    背景（v1.4.17）：该工具原先写死一批 v1.2.63 的特征符号，每次发版都无脑
    PASS，却打印"DMG 内含 v1.2.63 修复代码"—— 一个永不失败的检查等于没有检查。
    这里守的就是"不许再说超出能力的话"。
    """

    @classmethod
    def setUpClass(cls):
        cls.tool = _load("verify_dmg_pyz")
        cls.code = compile(
            "APP_VERSION = '9.9.9'\nGAP = 2\nDERIVED = GAP + 1\n\n\n"
            "def helper():\n    return worker()\n", "<m>", "exec")
        cls.old_code = compile("APP_VERSION = '8.8.8'\n", "<m2>", "exec")

    def _bundle(self, version="9.9.9", constants=None):
        tool = self.tool
        modules = {name: self.code for name in
                   ("stock_float_window",) + tool.CORE_MODULES}
        if constants is not None:
            modules["sf_constants"] = constants

        class _Fake:
            name = "Fake.app"
            pyz_size = 1

            def __init__(self):
                self.version = version

            def entries(self):
                return set(modules)

            def code(self, modname):
                return modules.get(modname)

        return _Fake()

    def _labels(self, results):
        return {label for label, passed, _ in results if not passed}

    # ---- 参数解析 ----
    def test_feature_specs_need_module_and_symbol(self):
        self.assertEqual(self.tool.parse_feature(["a::b"], "--check"), [("a", "b")])
        for bad in ("a", "a::", "::b", "  ::  ", ""):
            with self.assertRaises(SystemExit, msg=repr(bad)):
                self.tool.parse_feature([bad], "--check")

    def test_symbol_hits_see_through_nested_functions(self):
        hits = self.tool.symbol_hits
        self.assertEqual(hits(self.code, "worker"), 1, "内层函数调用的名字也要数到")
        self.assertGreaterEqual(hits(self.code, "helper"), 1)
        self.assertEqual(hits(self.code, "no_such_symbol"), 0)

    def test_top_level_literal_reads_small_int_and_derived(self):
        lit = self.tool.top_level_literal
        self.assertEqual(lit(self.code, "APP_VERSION"), "9.9.9")
        self.assertEqual(lit(self.code, "GAP"), 2, "3.14 的小整数必须读得出真值")
        self.assertIsNone(lit(self.code, "DERIVED"), "表达式赋值不得给出值")
        self.assertIsNone(lit(self.code, "NOPE"))

    # ---- 判定矩阵 ----
    def test_consistent_bundle_with_features_has_no_failure(self):
        results = self.tool.run_checks(self._bundle(), [("sf_ui_render", "worker")],
                                       [("sf_ui_render", "no_such_symbol")], "9.9.9")
        self.assertEqual(self._labels(results), set(),
                         "结构与版本同源、特征命中时不应有任何 FAIL")

    def test_missing_feature_and_unremoved_symbol_are_failures(self):
        results = self.tool.run_checks(self._bundle(),
                                        [("sf_ui_render", "nope")],
                                        [("sf_ui_render", "GAP")], None)
        self.assertEqual(len(self._labels(results)), 2)

    def test_absent_module_is_reported_not_swallowed(self):
        bundle = self._bundle()
        bundle.entries = lambda: {"sf_constants"}
        results = self.tool.run_checks(bundle, [("gone_module", "anything")], [], None)
        labels = self._labels(results)
        self.assertTrue(any("gone_module" in label for label in labels),
                        "模块不在包内必须 FAIL，不能当成「这条没特征可查」跳过")

    def test_version_desync_between_plist_and_bytecode_is_failure(self):
        results = self.tool.run_checks(self._bundle(constants=self.old_code),
                                       [], [], None)
        self.assertTrue(any("APP_VERSION 与 Info.plist 同源" in label
                            for label in self._labels(results)),
                        "包内常量与 Info.plist 不一致必须红")

    def test_expected_version_mismatch_is_failure(self):
        ok = self.tool.run_checks(self._bundle(), [], [], "9.9.9")
        self.assertEqual(self._labels(ok), set())
        bad = self.tool.run_checks(self._bundle(), [], [], "1.0.0")
        self.assertTrue(any("期望版本" in label for label in self._labels(bad)))

    # ---- 结论文案 ----
    def test_conclusion_refuses_to_overclaim_without_features(self):
        without = self.tool.conclusion(0, False)
        self.assertIn("不能证明", without)
        self.assertNotIn("全部命中", without)
        self.assertIn("全部命中", self.tool.conclusion(0, True))
        self.assertIn("FAIL", self.tool.conclusion(3, True))

    # ---- 真产物入口的失败姿势 ----
    def test_bundle_rejects_unreadable_or_half_baked_app(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = pathlib.Path(tmp) / "NoWhere.app"
            with self.assertRaises(SystemExit):
                self.tool.Bundle(missing)
            hollow = pathlib.Path(tmp) / "Hollow.app"
            (hollow / "Contents" / "MacOS").mkdir(parents=True)
            with (hollow / "Contents" / "Info.plist").open("wb") as f:
                plistlib.dump({"CFBundleExecutable": "HollowExec"}, f)
            with self.assertRaises(SystemExit):
                self.tool.Bundle(hollow)


if __name__ == "__main__":
    unittest.main()
