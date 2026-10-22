# -*- coding: utf-8 -*-
"""v1.4.23 临时目录旧副本自愈清扫回归。

背景：2026-09-22 / 09-23 两份"程序意外退出"报告（均为 v1.3.97）的崩溃路径都是
/private/tmp/*/实时股价-目标价版.app —— 历史遗留的旧副本被系统从临时目录重新拉起，
在 PyQt6 的 dlopen 静态初始化里 SIGSEGV，早于本程序任何 Python 代码。v1.4.5 的
sf_bootstrap 守卫只存在于新产物内，管不到 ≤1.4.4 的残留，因此由正式版启动后把这些
目录改名中性化。本文件守的是"清扫做对了、且绝不伤到别的东西"。

全部为行为断言：只看目录是否还在、名字变成什么、线程起没起、异常有没有外泄。

v1.4.24 按复核结论补三组：F1 重叠根只处理一次且不写假故障日志、
F2 `SF_ALLOW_TEMP_LAUNCH` 放行时产品侧不清扫、F3 真实 ps 输出必须能解析出正在运行的
bundle（原来只测"认得出来就会跳过"，没测"到底认不认得出来"）。
"""

import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sf_temp_sweep as sweep
import stock_float_window as m


def _make_app(parent, name, bundle_id, version="1.3.97", with_binary=True):
    """造一个最小 .app：Contents/Info.plist（可选）+ Contents/MacOS/可执行文件。"""
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


class _TmpCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="sf_sweep_")
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name


class TestSweepBehaviour(_TmpCase):
    def test_own_copy_is_neutralized_not_deleted(self):
        app = _make_app(self.root, sweep.APP_DIR_NAME, sweep.BUNDLE_ID)
        self.assertEqual(sweep.sweep_temporary_copies(roots=[self.root]), 1)
        self.assertFalse(os.path.exists(app), "旧副本目录应已改名")
        self.assertTrue(os.path.isdir(app + ".stale"), "只改名，内容必须仍在")
        self.assertTrue(os.path.isfile(
            os.path.join(app + ".stale", "Contents", "MacOS",
                         "StockFloatTargetPrice")))

    def test_foreign_app_is_left_alone(self):
        other = _make_app(self.root, "别的应用.app", "com.example.other")
        self.assertEqual(sweep.sweep_temporary_copies(roots=[self.root]), 0)
        self.assertTrue(os.path.isdir(other))

    def test_empty_shell_is_left_alone(self):
        # 测试夹具/打包脚本会造只有空目录的 .app，系统不会当应用，没必要改名。
        shell = _make_app(self.root, sweep.APP_DIR_NAME, sweep.BUNDLE_ID,
                          with_binary=False)
        self.assertEqual(sweep.sweep_temporary_copies(roots=[self.root]), 0)
        self.assertTrue(os.path.isdir(shell))

    def test_running_copy_is_skipped(self):
        app = _make_app(self.root, sweep.APP_DIR_NAME, sweep.BUNDLE_ID)
        with patch.object(sweep, "running_bundle_paths",
                          lambda: {os.path.realpath(app)}):
            self.assertEqual(sweep.sweep_temporary_copies(roots=[self.root]), 0)
        self.assertTrue(os.path.isdir(app), "正在运行的副本不得被改名")

    def test_rename_failure_is_logged_and_not_raised(self):
        _make_app(self.root, sweep.APP_DIR_NAME, sweep.BUNDLE_ID)
        messages = []
        with patch.object(sweep, "neutralize",
                          side_effect=OSError("Operation not permitted")):
            changed = sweep.sweep_temporary_copies(roots=[self.root],
                                                   log=messages.append)
        self.assertEqual(changed, 0)
        self.assertTrue(any("rename failed" in text for text in messages),
                        "改名失败必须留痕，否则线上无从判断清扫是否生效")

    def test_scan_without_log_callback_is_safe(self):
        _make_app(self.root, sweep.APP_DIR_NAME, sweep.BUNDLE_ID)
        self.assertEqual(sweep.sweep_temporary_copies(roots=[self.root],
                                                      log=None), 1)


class TestWindowWiring(_TmpCase):
    """主窗的调度：只对冻结产物生效、跑在后台线程、异常不外泄。"""

    class _Stub:
        _threads_stopped = False

    def _call(self, frozen, sweep_result=None, sweep_error=None):
        """返回 (线程名列表, 清扫被调用次数)。线程 target 同步执行以便断言。"""
        started = []

        def _sweep(**kwargs):
            if sweep_error is not None:
                raise sweep_error
            return sweep_result

        class _Thread:
            def __init__(self, target=None, name=None, **kwargs):
                started.append(name)
                self._target = target

            def start(self):
                self._target()

        with patch.object(sys, "frozen", frozen, create=True), \
                patch.object(m.threading, "Thread", _Thread), \
                patch.object(sweep, "sweep_temporary_copies", _sweep), \
                patch.object(m, "log_line", lambda *a, **k: None):
            m.FloatWindow._sweep_temp_copies(self._Stub())
        return started

    def test_source_run_starts_nothing(self):
        self.assertEqual(self._call(frozen=False), [],
                         "源码运行/测试不该去动真机的临时目录")

    def test_stopped_threads_start_nothing(self):
        stub = self._Stub()
        stub._threads_stopped = True

        class _NoThread:
            def __init__(self, *a, **k):
                raise AssertionError("退出阶段不应再新建后台线程")

        with patch.object(sys, "frozen", True, create=True), \
                patch.object(m.threading, "Thread", _NoThread):
            m.FloatWindow._sweep_temp_copies(stub)

    def test_frozen_run_sweeps_in_background_thread(self):
        started = self._call(frozen=True, sweep_result=2)
        self.assertEqual(started, ["temp-sweep"])

    def test_worker_exception_does_not_escape(self):
        # 外泄会让行情主流程所在的进程多一条未捕获异常；这里要求静默降级。
        self.assertEqual(self._call(frozen=True, sweep_error=OSError("boom")),
                         ["temp-sweep"])


class TestOverlappingRoots(unittest.TestCase):
    """v1.4.24 复核 F1：根目录重叠时一份副本只能被处理一次。

    $TMPDIR 本身就位于 /private/var/folders 之内，两个根各扫一遍会让同一副本命中
    两次，第二遍对着已消失的路径再改名 → FileNotFoundError 被记成"改名失败"，
    往用户真实 debug.log 灌误导日志。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="sf_sweep_")
        self.addCleanup(self._tmp.cleanup)
        self.outer = os.path.join(self._tmp.name, "var_folders")
        self.inner = os.path.join(self.outer, "wy", "T")
        os.makedirs(self.inner)
        self.app = _make_app(self.inner, sweep.APP_DIR_NAME, sweep.BUNDLE_ID)

    def test_tmpdir_stays_a_root_for_depth_budget(self):
        """$TMPDIR 必须仍是独立根：只靠 /private/var/folders 扫的话，前缀就吃掉了
        DEFAULT_MAX_DEPTH 的 4 层，深层嵌套副本会被漏掉（v1.4.24 实测踩过）。"""
        self.assertIn(os.path.realpath(tempfile.gettempdir()), sweep.temp_roots())

    def test_deeply_nested_copy_under_tmpdir_is_found(self):
        """$TMPDIR 作为独立根时，它下面的深层副本必须被扫到；挂在外层根下则会被深度预算漏掉。

        这条用例过去直接扫真实的 temp_roots()：本机 $TMPDIR 顶层有十几万项，遍历一次要
        几十秒，整档带覆盖率时超过单用例 60 秒上限，runner 被超时打断（2026-10-22 提交
        被拦）。判据本身不需要真实目录，所以把 $TMPDIR 指到用例自己的临时目录里，
        只扫"那一条根"，既不碰真实目录，也仍能证明 temp_roots() 给出的就是这一条独立根。
        """
        # 深度全部从 DEFAULT_MAX_DEPTH 推出来，不写死 6：改了预算这条用例仍然成立。
        chain = [str(i) for i in range(sweep.DEFAULT_MAX_DEPTH - 2)]
        deep_parent = os.path.join(self.inner, *chain)
        os.makedirs(deep_parent)
        deep = _make_app(deep_parent, sweep.APP_DIR_NAME, sweep.BUNDLE_ID)
        with patch.object(tempfile, "gettempdir", return_value=self.inner):
            inner_real = os.path.realpath(self.inner)
            roots = sweep.temp_roots()
            self.assertIn(inner_real, roots, "$TMPDIR 必须仍是一条独立的扫描根")
            hits = {os.path.realpath(path)
                    for path, _reason in sweep.find_stale_apps([inner_real])}
        self.assertIn(os.path.realpath(deep), hits,
                      "$TMPDIR 下深层副本必须仍被扫到")
        # 对照：同一份副本若只经外层根（把 $TMPDIR 之前的前缀也算进深度）就超预算被漏掉，
        # 这正是必须单独保留 $TMPDIR 这条根的原因；对照不成立说明本用例的深度构造失效。
        via_outer = {os.path.realpath(path)
                     for path, _reason in sweep.find_stale_apps([os.path.realpath(self.outer)])}
        self.assertNotIn(os.path.realpath(deep), via_outer,
                         "深度构造失效：副本在外层根下本来就能被扫到，就证明不了独立根的必要性")

    def test_overlapping_roots_yield_single_hit(self):
        hits = sweep.find_stale_apps([self.outer, self.inner])
        self.assertEqual(len(hits), 1, "重叠根必须按 realpath 去重: %s" % hits)

    def test_overlapping_roots_log_no_spurious_failure(self):
        messages = []
        changed = sweep.sweep_temporary_copies(roots=[self.outer, self.inner],
                                               log=messages.append)
        self.assertEqual(changed, 1)
        self.assertFalse([m for m in messages if "rename failed" in m],
                         "第二遍改名失败是重复扫描造成的假故障: %s" % messages)


class TestPsBundleParsing(unittest.TestCase):
    """v1.4.24 复核 F3：守"真的能从 ps 认出正在运行的 bundle"，而不是只测跳过分支。

    本应用 bundle 名是中文，ps 输出格式/编码一变，解析就会静默返回空集，
    那条"跳过正在运行副本"的保护网随之消失且无人报警 —— 这里把它变成会报警。
    """

    SAMPLE = "\n".join([
        "/System/Library/CoreServices/Finder.app/Contents/MacOS/Finder",
        "/Applications/实时股价-目标价版.app/Contents/MacOS/StockFloatTargetPrice",
        "/Applications/Some App With Spaces.app/Contents/MacOS/Some App",
        "/usr/libexec/containermanagerd",
        "/Library/Input Methods/WeType.app/Contents/MacOS/WeType",
        "",
    ])

    def test_parser_extracts_bundles_and_ignores_non_bundles(self):
        got = sweep.bundle_paths_from_ps(self.SAMPLE)
        self.assertEqual(got, {
            "/System/Library/CoreServices/Finder.app",
            "/Applications/实时股价-目标价版.app",
            "/Applications/Some App With Spaces.app",
            "/Library/Input Methods/WeType.app",
        })

    def test_parser_handles_chinese_and_spaces(self):
        """本应用自身的 bundle 名是中文 —— 这条一旦退化，保护网就静默失效。"""
        got = sweep.bundle_paths_from_ps(self.SAMPLE)
        self.assertIn("/Applications/实时股价-目标价版.app", got)

    def test_live_ps_output_parses_something(self):
        """不 mock：真实 ps 必须能解析出至少一个 bundle（Finder 常驻）。"""
        self.assertIn("/System/Library/CoreServices/Finder.app",
                      sweep.running_bundle_paths(),
                      "ps 输出格式或解析逻辑变了，'跳过正在运行副本'已静默失效")

    def test_installed_copy_is_parsed_from_one_snapshot(self):
        """安装位置那份必须被认出来 —— 判定与断言都取自同一份进程表快照。

        旧写法（2026-10-21 随机顺序档实测红在此）有两条洞：
        ① 用例自己查一次 ps、再让产品函数查一次：两次快照之间只要有人启动或退出
           正式版，断言量的就不是同一件事，于是"偶发红"取决于机器上此刻有谁在跑；
        ② 按「app 名字出现在 ps 里」判"正式版在跑"：同一款程序可以同时从
           `/Applications` 与发布树的 `dist/` 里跑，那是两个不同路径 —— 本轮就是
           验包时 dist/ 那份在跑，被当成安装版在跑却断言找不到安装路径。
        真实 ps 输出格式的覆盖在 `test_live_ps_output_parses_something`（Finder 常驻）。
        """
        installed = os.path.realpath("/Applications/%s" % sweep.APP_DIR_NAME)
        text = ("/System/Library/CoreServices/Finder.app/Contents/MacOS/Finder\n"
                "%s/Contents/MacOS/whatever\n" % installed)

        class _PS:
            stdout = text

        with patch.object(sweep.subprocess, "run", return_value=_PS()):
            self.assertIn(installed, sweep.running_bundle_paths())

    @unittest.skipUnless(os.path.isdir("/Applications/%s" % sweep.APP_DIR_NAME),
                         "本机未安装正式版")
    def test_running_official_app_is_detected(self):
        """本机安装版此刻确实在跑时，产品函数要从**一次**采样里报出安装路径。

        这一条是环境相关的补充核验：不在跑就带理由跳过，绝不因为"没在跑"判通过。
        """
        text = subprocess.run(["/bin/ps", "-Ao", "comm="],
                              capture_output=True, text=True).stdout
        installed = os.path.realpath("/Applications/%s" % sweep.APP_DIR_NAME)
        if installed not in text:
            other = sorted({p for p in sweep.bundle_paths_from_ps(text)
                            if sweep.APP_DIR_NAME in p})
            self.skipTest("正式版未从安装位置运行（本机在跑的那份：%s）"
                          % (", ".join(other) or "无"))
        self.assertIn(installed, sweep.bundle_paths_from_ps(text))


class TestSweepSwitch(unittest.TestCase):
    """v1.4.24 复核 F2：核验通道 SF_ALLOW_TEMP_LAUNCH 必须同时关掉产品侧清扫。"""

    class _Stub:
        _threads_stopped = False

    def _started_threads(self, env):
        started = []

        class _Thread:
            def __init__(self, target=None, name=None, **kwargs):
                started.append(name)

            def start(self):
                pass

        with patch.object(sys, "frozen", True, create=True), \
                patch.object(m.threading, "Thread", _Thread), \
                patch.dict(os.environ, {"SF_ALLOW_TEMP_LAUNCH": env}), \
                patch.object(m, "log_line", lambda *a, **k: None):
            m.FloatWindow._sweep_temp_copies(self._Stub())
        return started

    def test_switch_offs_the_sweep(self):
        self.assertEqual(self._started_threads("1"), [],
                         "核验者放行时不得清扫，否则会把待启动的副本改名")

    def test_default_still_sweeps(self):
        self.assertEqual(self._started_threads(""), ["temp-sweep"])


class TestScanScope(unittest.TestCase):
    def test_roots_are_temp_only(self):
        roots = sweep.temp_roots()
        self.assertTrue(roots, "应至少识别出一个临时目录")
        for root in roots:
            self.assertTrue(
                root.startswith(("/private/tmp", "/private/var")),
                "扫描根必须限定在系统临时目录，实际: %s" % root)

    def test_installed_app_is_never_a_root(self):
        self.assertNotIn("/Applications", [os.path.basename(r)
                                           for r in sweep.temp_roots()])


if __name__ == "__main__":
    unittest.main()
