# -*- coding: utf-8 -*-
"""发版核验工具补测：未定义名扫描的命令行面，与产物解包的取码逻辑。

为什么单独补这一层（本轮为扩大自动化覆盖而加）
----------------------------------------------
这两个脚本本身**就是关卡**：`undefined_name_scan` 被提交钩子和
test_arch_constraints 共用，`verify_dmg_pyz` 是发版时回答"这次修复到底进没进
产物"的唯一手段。关卡自己坏掉的表现是"永远 PASS"——本仓已经为这种检查吃过
好几次亏（见 verify_dmg_pyz 的 docstring）。所以它们的主入口与解包分支也要有测试。

口径：不依赖已打包的 .app（产物有无、新旧都不该影响单测），全部用合成目录、
合成 plist 和合成字节码。
"""
import json
import marshal
import os
import pathlib
import plistlib
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import undefined_name_scan as U          # noqa: E402
import verify_dmg_pyz as V               # noqa: E402


def _load(name):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------
# undefined_name_scan：CLI
# --------------------------------------------------------------------------

class TestUndefinedNameScanCli(unittest.TestCase):
    def setUp(self):
        self.d = pathlib.Path(tempfile.mkdtemp())

    def _write(self, name, src):
        (self.d / name).write_text(src, encoding="utf-8")
        return self.d / name

    def _main(self, *argv):
        buf = []
        with mock.patch("builtins.print", side_effect=lambda *a: buf.append(
                " ".join(str(x) for x in a))):
            code = U.main(list(argv) + ["--root", str(self.d)])
        return code, "\n".join(buf)

    def test_clean_tree_exits_zero_without_strict_noise(self):
        self._write("good.py", "import os\n\n\ndef f():\n    return os.sep\n")
        code, out = self._main()
        self.assertEqual(code, 0)
        self.assertIn("未定义名合计：0 处", out)

    def test_a_planted_name_is_listed_with_file_line_and_name(self):
        self._write("bad.py", "def f():\n    return nowhere_xyz(1)\n")
        code, out = self._main()
        self.assertEqual(code, 0, "默认不严格：只报不改退出码")
        self.assertIn("bad.py:2", out)
        self.assertIn("nowhere_xyz", out)
        self.assertIn("未定义名合计：1 处", out)

    def test_strict_turns_hits_into_a_nonzero_exit(self):
        self._write("bad.py", "def f():\n    return nowhere_xyz(1)\n")
        code, _ = self._main("--strict")
        self.assertEqual(code, 1, "--strict 是提交钩子的判据，必须红")

    def test_strict_on_a_clean_tree_still_exits_zero(self):
        self._write("good.py", "x = 1\n")
        self.assertEqual(self._main("--strict")[0], 0)

    def test_unparsable_file_is_reported_as_skipped_not_silently_passed(self):
        """解析失败=这个文件根本没被检查过，必须显式说出口。"""
        self._write("broken.py", "def f(:\n")
        code, out = self._main("--strict")
        self.assertIn("[跳过] broken.py", out)
        self.assertIn("SyntaxError", out)
        self.assertEqual(code, 0, "语法错不算未定义名，但它自己会被另一道关卡拦住")

    def test_dynamic_and_star_imports_are_flagged_as_incomplete(self):
        """这段源码只是**扫描器的输入文本**，全程不被执行。

        扫描器要认的就是"名字可能运行时才出现"这一类写法，
        所以判定结论必须标"不完备"，由人确认而不是假装干净。
        """
        self._write("dyn.py",
                    "from os import *\n\n\ndef f():\n    return eval('sep')\n")
        code, out = self._main()
        self.assertIn("判定不完备", out)
        self.assertIn("含 exec/eval/globals", out)
        self.assertIn("含 import *", out)
        self.assertEqual(code, 0)

    def test_json_output_is_the_full_detail_for_machines(self):
        self._write("bad.py", "def f():\n    return nowhere_xyz(1)\n")
        target = self.d / "out.json"
        with mock.patch("builtins.print"):
            U.main(["--root", str(self.d), "--json", str(target)])
        data = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual([r["file"] for r in data], ["bad.py"])
        self.assertEqual(data[0]["hits"], [{"name": "nowhere_xyz", "line": 2}])
        self.assertIsNone(data[0]["error"])
        self.assertFalse(data[0]["dynamic"])

    def test_historical_one_off_build_scripts_stay_out_of_scope(self):
        """PRODUCT_SKIP 里的旧打包脚本硬编码了别机路径，扫它们只会制造红灯。"""
        self._write("build_v1240.py", "def f():\n    return nowhere_xyz(1)\n")
        code, out = self._main("--strict")
        self.assertEqual(code, 0)
        self.assertIn("产品模块 0 个文件", out)

    def test_the_real_product_tree_still_scans_clean(self):
        """关卡对自己扫描的真实目标负责：产品模块必须 0 命中。"""
        files = [p for p in ROOT.glob("*.py") if p.name not in U.PRODUCT_SKIP]
        self.assertGreater(len(files), 20, "扫到 0 个文件等于没扫")
        hits = []
        for path in files:
            r = U.scan(path)
            self.assertIsNone(r["error"], "%s 解析失败 → 关卡对它无效" % path.name)
            hits += ["%s:%d %s" % (path.name, h["line"], h["name"])
                     for h in r["hits"]]
        self.assertEqual(hits, [], "产品模块出现未定义名：%s" % hits)

    def test_undecodable_bytes_are_scanned_rather_than_skipped(self):
        """errors="replace"：坏字节不能让这个文件悄悄逃过关卡。"""
        (self.d / "latin.py").write_bytes(
            b"x = 1\ndef f():\n    return nowhere_xyz(1)\n")
        code, out = self._main("--strict")
        self.assertEqual(code, 1)
        self.assertIn("nowhere_xyz", out)


# --------------------------------------------------------------------------
# verify_dmg_pyz：Bundle 解包
# --------------------------------------------------------------------------

def _fake_bundle(tmp):
    """造一个"看起来像 .app"的目录：plist + 可执行文件占位。"""
    app = pathlib.Path(tmp) / "Fake.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    with (app / "Contents" / "Info.plist").open("wb") as f:
        plistlib.dump({"CFBundleExecutable": "FakeExec",
                       "CFBundleName": "Fake",
                       "CFBundleShortVersionString": "9.9.9"}, f)
    (app / "Contents" / "MacOS" / "FakeExec").write_bytes(b"\x00mach-o")
    return app


class TestBundleCodeExtraction(unittest.TestCase):
    """`Bundle.code` 必须适配当前 PyInstaller 两个 reader 的真实返回形状。

    实测口径（PyInstaller 的 readers.py）：
      · ZlibArchiveReader.extract 已经把模块 marshal 成 **code 对象**；
      · CArchiveReader.extract 给的是 **bytes**（入口脚本走这条路）；
      · 命名空间包返回 None；缺条目抛 KeyError；解不开抛 ImportError。
    用 __new__ 绕过真实归档：单测不该依赖 dist/ 里有没有产物，也不该去解
    一个几十 MB 的真 PYZ（那是发版核验的活，不是单测的活）。
    """

    def _bundle(self, pz=None, ca=None):
        b = V.Bundle.__new__(V.Bundle)
        b._pz = pz if pz is not None else types.SimpleNamespace(
            extract=_missing, toc={})
        b._ca = ca if ca is not None else types.SimpleNamespace(
            extract=_missing, toc=[])
        b._cache = {}
        return b

    def test_pyz_code_object_is_passed_through_untouched(self):
        code = compile("APP_VERSION = '9.9.9'", "<m>", "exec")
        b = self._bundle(pz=types.SimpleNamespace(
            extract=lambda name: code, toc={}))
        self.assertIs(b.code("sf_constants"), code)

    def test_carchive_bytes_are_unmarshalled(self):
        code = compile("def main():\n    return helper()", "<e>", "exec")
        b = self._bundle(
            ca=types.SimpleNamespace(extract=lambda name: marshal.dumps(code),
                                     toc=["PYZ"]))
        self.assertIn("helper", V.names(b.code("stock_float_window")))

    def test_a_namespace_package_falls_through_to_the_carchive(self):
        """命名空间包 extract 返回 None：不能把它当成"取到了"就停手。"""
        code = compile("x = 1", "<c>", "exec")
        calls = []

        def ca_extract(name):
            calls.append(name)
            return code

        b = self._bundle(pz=types.SimpleNamespace(extract=lambda n: None,
                                                  toc={}),
                         ca=types.SimpleNamespace(extract=ca_extract,
                                                  toc=["PYZ"]))
        self.assertIs(b.code("mod"), code)
        self.assertEqual(calls, ["mod"])

    def test_absent_in_both_archives_returns_none(self):
        self.assertIsNone(self._bundle().code("gone"))

    def test_result_is_cached(self):
        calls = []

        def extract(name):
            calls.append(name)
            return compile("x = 1", "<c>", "exec")

        b = self._bundle(pz=types.SimpleNamespace(extract=extract, toc={}))
        first = b.code("mod")
        self.assertIs(b.code("mod"), first)
        self.assertEqual(len(calls), 1, "重复解包会让核验慢一倍")

    def test_corrupt_payload_raises_so_run_checks_can_report_it(self):
        b = self._bundle(ca=types.SimpleNamespace(
            extract=lambda name: b"\x00not-a-code-object", toc=["PYZ"]))
        with self.assertRaises(ValueError):
            b.code("mod")

    def test_run_checks_turns_an_unreadable_module_into_a_failing_row(self):
        """核验工具最怕"崩在半路却看起来像通过"：异常必须变成一条 FAIL。

        口径边界：结构自检循环外层有 try/except，所以非 sf_constants 的模块
        解不开会被记成 FAIL；sf_constants 本身解不开目前会直接抛出（版本同源
        那段没有兜底），已在 2026-09-24 的覆盖报告里记为待办。
        """
        ok = compile("APP_VERSION = '9.9.9'\n", "<c>", "exec")

        class _Partial:
            version = "9.9.9"

            def entries(self):
                return {"a"}

            def code(self, mod):
                if mod == "renderer":
                    raise ImportError("Failed to unmarshal PYZ entry")
                return ok

        results = V.run_checks(_Partial(), [], [], None)
        failed = [r for r in results if not r[1]]
        self.assertTrue(any("可反序列化" in label for label, _, _ in failed),
                        results)
        self.assertTrue(any("ImportError" in detail for _, _, detail in failed),
                        "异常原文要留在详情里，否则不知道该查哪个模块")

    def test_entries_merge_both_archives(self):
        b = self._bundle(pz=types.SimpleNamespace(extract=_missing,
                                                  toc={"a": 1, "b": 2}),
                         ca=types.SimpleNamespace(extract=_missing,
                                                  toc=["b", "c"]))
        self.assertEqual(b.entries(), {"a", "b", "c"})

    def test_a_pyz_without_a_toc_is_not_a_crash(self):
        b = self._bundle(pz=types.SimpleNamespace(extract=_missing))
        self.assertEqual(b.entries(), set())


def _missing(name):
    raise KeyError(name)


class TestNamesHelper(unittest.TestCase):
    CODE = compile(
        "TOP = 1\n"
        "def outer():\n"
        "    return inner(1)\n"
        "class C:\n"
        "    def meth(self):\n"
        "        return deep()\n", "<m>", "exec")

    def test_nested_code_objects_are_walked(self):
        names = V.names(self.CODE)
        self.assertIn("TOP", names, "模块级赋值的目标名也要收")
        self.assertIn("inner", names, "一层函数")
        self.assertIn("deep", names, "两层：类里的方法")

    def test_missing_names_simply_absent(self):
        self.assertNotIn("never_used_anywhere", V.names(self.CODE))

    def test_symbol_hits_count_occurrences_across_scopes(self):
        code = compile("def a():\n    return go()\n\n\ndef b():\n    return go()\n",
                       "<c>", "exec")
        self.assertEqual(V.symbol_hits(code, "go"), 2)
        self.assertEqual(V.symbol_hits(code, "nope"), 0)


class TestMainEntry(unittest.TestCase):
    """main() 的退出码就是发版脚本的判据，必须与结论一致。"""

    def _bundle(self, version="9.9.9"):
        modules = {name: V.__dict__ and compile(
            "APP_VERSION = '%s'\nMARKER = 1\n" % version, "<m>", "exec")
            for name in ("stock_float_window",) + V.CORE_MODULES}

        class _B:
            name = "Fake"
            pyz_size = 2048
            path = pathlib.Path("/tmp/Fake.app")

            def __init__(self, path):
                self.version = version

            def dispose(self):
                pass

            def entries(self):
                return set(modules)

            def code(self, mod):
                return modules.get(mod)

        return _B

    def test_zero_failures_exits_zero_and_the_feature_claim_is_earned(self):
        with mock.patch.object(V, "Bundle", self._bundle()):
            buf = []
            with mock.patch("builtins.print", side_effect=lambda *a: buf.append(
                    " ".join(str(x) for x in a))):
                code = V.main(["app", "--check", "sf_ui_render::MARKER"])
        out = "\n".join(buf)
        self.assertEqual(code, 0)
        self.assertIn("本轮特征: --check 1 条", out)
        self.assertIn("全部命中", out)
        self.assertIn("PASS", out)

    def test_a_missing_feature_exits_nonzero(self):
        with mock.patch.object(V, "Bundle", self._bundle()):
            with mock.patch("builtins.print"):
                code = V.main(["app", "--check", "sf_ui_render::not_there"])
        self.assertEqual(code, 1, "特征没命中却退 0 = 发版核验白做")

    def test_failure_wins_over_the_no_features_wording(self):
        with mock.patch.object(V, "Bundle", self._bundle()):
            with mock.patch("builtins.print"):
                code = V.main(["app", "--forbid", "sf_ui_render::MARKER"])
        self.assertEqual(code, 1)

    def test_without_features_the_conclusion_refuses_to_overclaim(self):
        with mock.patch.object(V, "Bundle", self._bundle()):
            buf = []
            with mock.patch("builtins.print", side_effect=lambda *a: buf.append(
                    " ".join(str(x) for x in a))):
                code = V.main(["app"])
        out = "\n".join(buf)
        self.assertEqual(code, 0)
        self.assertIn("不能证明本次修复在包内", out)

    def test_bad_feature_syntax_refuses_to_run(self):
        with mock.patch("builtins.print"):
            with self.assertRaises(SystemExit):
                V.main(["app", "--check", "no_colon"])

    def test_dispose_runs_even_when_the_checks_blow_up(self):
        """临时目录不清的话，每次发版核验都在 /tmp 留一份几十 MB 的 PYZ。"""
        disposed = []

        bundle_cls = self._bundle()

        class _Wrap(bundle_cls):
            def code(self, mod):
                raise RuntimeError("corrupt archive")

            def dispose(self):
                disposed.append(1)
                raise RuntimeError("dispose must not mask the verdict")

        with mock.patch.object(V, "Bundle", _Wrap), \
                mock.patch("builtins.print"):
            with self.assertRaises(RuntimeError):
                V.main(["app", "--check", "sf_ui_render::MARKER"])
        self.assertEqual(disposed, [1])


class TestPlistGuards(unittest.TestCase):
    def test_missing_plist_is_a_hard_refusal(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit):
                V.read_info_plist(pathlib.Path(tmp) / "Nope.app")

    def test_a_bundle_without_an_executable_name_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = pathlib.Path(tmp) / "NoExec.app"
            (app / "Contents").mkdir(parents=True)
            with (app / "Contents" / "Info.plist").open("wb") as f:
                plistlib.dump({"CFBundleName": "NoExec"}, f)
            with self.assertRaises(SystemExit):
                V.bundle_executable(app)

    def test_bundle_disposes_its_temp_dir_when_the_pyz_is_absent(self):
        """没有 PYZ 就直接拒绝，但不能把临时目录留在 /tmp。"""
        with tempfile.TemporaryDirectory() as tmp:
            app = _fake_bundle(tmp)

            class _NoPyz:
                toc = ["nothing"]

                def __init__(self, path):
                    pass

                def extract(self, name):
                    raise KeyError(name)

            created = []
            real_mkdtemp = tempfile.mkdtemp

            def spy(*a, **k):
                d = real_mkdtemp(*a, **k)
                created.append(d)
                return d

            class _NoPyzReader:
                toc = ["just-struct", "base_library.zip"]

                def __init__(self, path):
                    pass

                def extract(self, name):
                    raise KeyError(name)

            with mock.patch.object(V, "CArchiveReader", _NoPyzReader), \
                    mock.patch.object(tempfile, "mkdtemp", spy):
                with self.assertRaises(SystemExit) as cm:
                    V.Bundle(app)
            self.assertIn("未找到 PYZ", str(cm.exception))
            self.assertEqual(len(created), 1)
            self.assertFalse(os.path.exists(created[0]),
                             "拒绝执行时也必须把自己建的临时目录清掉")


class TestRunnerUiDialogGuard(unittest.TestCase):
    """运行器的第⑤道兜底：任何用例真去弹系统窗口都得被拦下并记为违规。

    真实教训（见 tools/test_runner.py 注释）：一条漏打桩的用例让用户桌面
    在每次提交时凭空弹出「软件更新」窗口。
    """

    def setUp(self):
        self.runner = _load("test_runner")
        import subprocess
        self._real_popen = subprocess.Popen
        self._real_run = subprocess.run
        self._saved_violations = list(self.runner.UI_DIALOG_VIOLATIONS)

    def tearDown(self):
        import subprocess
        subprocess.Popen = self._real_popen
        subprocess.run = self._real_run
        self.runner.UI_DIALOG_VIOLATIONS[:] = self._saved_violations

    # ---- 判定口径 ----
    def test_dialog_and_notification_commands_are_recognised(self):
        d = self.runner.describe_ui_command
        for marker in ("display dialog", "display notification",
                       "display alert"):
            self.assertIsNotNone(d(["osascript", "-e", marker + ' "x"']), marker)

    def test_everything_else_is_left_alone(self):
        """放行口径必须宽：误伤会把依赖真实子进程的用例（假 Popen、git、hdiutil）全打红。"""
        d = self.runner.describe_ui_command
        self.assertIsNone(d(["git", "status"]))
        self.assertIsNone(d([sys.executable, "child.py"]))
        self.assertIsNone(d(["osascript", "-e", 'return "1"']),
                          "只用 osascript 取系统信息不算弹窗")
        self.assertIsNone(d(None))
        self.assertIsNone(d(b"/bin/echo hi"), "bytes 命令也要能判")

    def test_a_long_command_is_summarised_from_the_front(self):
        """摘要中段截断：固定脚本源码在前，标题/正文参数在后。"""
        long_cmd = ["osascript", "-e", "display dialog " + "字" * 400]
        hit = self.runner.describe_ui_command(long_cmd)
        self.assertIn("display dialog", hit)
        self.assertLess(len(hit), 260)
        self.assertNotIn("\n", hit)

    # ---- 拦截行为 ----
    def test_run_is_stopped_and_recorded_without_killing_the_case(self):
        import subprocess
        self.runner.install_ui_dialog_guard()
        self.runner.UI_DIALOG_VIOLATIONS.clear()
        result = subprocess.run(["osascript", "-e",
                                 'display dialog "软件更新" buttons {"好"}'])
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(self.runner.UI_DIALOG_VIOLATIONS), 1,
                         "拦截必须留名，否则只表现为「弹窗不见了」")
        case, hit = self.runner.UI_DIALOG_VIOLATIONS[0]
        self.assertIn("display dialog", hit)

    def test_popen_raises_an_oserror_shaped_error(self):
        """刻意继承 OSError：sf_system_alert 对「osascript 不可用」就是吞 OSError 降级，

        拦截不该把用例拖成看不懂的崩溃线程栈。
        """
        import subprocess
        self.runner.install_ui_dialog_guard()
        self.runner.UI_DIALOG_VIOLATIONS.clear()
        with self.assertRaises(OSError) as cm:
            subprocess.Popen(["osascript", "-e", 'display notification "x"'])
        self.assertIsInstance(cm.exception, self.runner.UiDialogBlocked)

    def test_a_benign_subprocess_call_still_works_after_the_guard(self):
        import subprocess
        self.runner.install_ui_dialog_guard()
        self.runner.UI_DIALOG_VIOLATIONS.clear()
        out = subprocess.run([sys.executable, "-c", "print(1)"],
                             capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), "1")
        self.assertEqual(self.runner.UI_DIALOG_VIOLATIONS, [])

    def test_reporting_is_deduplicated_and_tells_which_case_did_it(self):
        self.runner.UI_DIALOG_VIOLATIONS[:] = [
            ("test_a (x.y)", 'osascript display dialog A')]
        self.runner.UI_DIALOG_VIOLATIONS.append(("test_a (x.y)",
                                                 'osascript display dialog A'))
        self.runner.UI_DIALOG_VIOLATIONS.append(("test_b (x.y)",
                                                 'osascript display dialog B'))
        buf = []
        with mock.patch("builtins.print", side_effect=lambda *a: buf.append(
                " ".join(str(x) for x in a))):
            self.assertTrue(self.runner._report_ui_dialog_violations())
        text = "\n".join(buf)
        self.assertIn("test_a", text)
        self.assertIn("test_b", text)
        self.assertEqual(text.count("osascript display dialog A"), 1,
                         "同一用例同一命令只报一次，否则真信号被刷屏淹没")

    def test_no_violations_reports_clean_and_false(self):
        self.runner.UI_DIALOG_VIOLATIONS.clear()
        with mock.patch("builtins.print") as pr:
            self.assertFalse(self.runner._report_ui_dialog_violations())
        self.assertEqual(pr.call_count, 0)


if __name__ == "__main__":
    unittest.main()
