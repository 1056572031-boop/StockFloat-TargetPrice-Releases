# -*- coding: utf-8 -*-
"""架构级约束关卡（AGENTS.md 第 6 节第 2 条指定的落点）。

这里放的是"守的不是某个功能、而是整仓书写纪律"的用例。之所以允许它读源码/AST
（平时禁止源码串断言），是因为**守的东西只能用这种方式守**：

  本文件未定义名关卡 —— 防的是"某功能从写下起一次也没执行过"。
  产品里有 206 处 `except …: pass`（2026-09-24 盘点），NameError 会被静默吞掉。
  真实事故：`stock_float_window._raise_tool_window` 裸调未导入的 `_objc_deps`，
  工具窗层级提升潜伏几十个版本从未生效（v1.4.29 复核发现并修复）。
"""
import ast
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import undefined_name_scan as U     # noqa: E402

try:
    from _base import SFCase                     # noqa: E402
except ImportError:                              # 以包形式被 discover 导入时
    from tests._base import SFCase               # noqa: E402


def _product_files():
    return sorted(p for p in ROOT.glob("*.py") if p.name not in U.PRODUCT_SKIP)


def _helper_files():
    """tools/ 与 tests/ 下的一级 .py（不含 tools/archive/，那是历史专项脚本）。

    为什么单独列：这两个目录**从来没进过扫描范围**（2026-09-24 实测），
    而出过"核验器说假话""探针把累计值当单场景"这类事故的恰好是 tools/。
    不扫它们，等于给最容易自欺的那一层留了盲区。
    """
    return sorted(list((ROOT / "tools").glob("*.py"))
                  + list((ROOT / "tests").glob("*.py")))


class TestNoUndefinedNamesInProductModules(unittest.TestCase):
    """产品模块里不允许出现"本文件从没绑定过"的名字。"""

    def test_scan_finds_nothing(self):
        bad = []
        for path in _product_files():
            r = U.analyze(path.name, path.read_text(encoding="utf-8", errors="replace"))
            if r["error"]:
                bad.append("%s 解析失败：%s" % (r["file"], r["error"]))
            for h in r["hits"]:
                bad.append("%s:%d 用了未定义的名字 %s" % (r["file"], h["line"], h["name"]))
        self.assertEqual(
            bad, [],
            "未定义名会被同文件里的 `except Exception: pass` 静默吞掉，"
            "功能就永久失效而没人知道（历史上真发生过）：\n  " + "\n  ".join(bad))

    def test_tools_and_tests_have_no_undefined_names(self):
        """盲区补齐：tools/ 与 tests/ 同样不许有未定义名。

        这两处的 .py 大多在**给人报结论**（核验器、探针、发布工具），本身写错名字
        时症状是"报了一切正常"，比产品里少一个功能更难发现。
        """
        files = _helper_files()
        self.assertGreater(len(files), 50,
                           "只取到 %d 个 tools/tests 文件，扫描范围疑似失效" % len(files))
        bad = []
        for path in files:
            r = U.analyze(path.name, path.read_text(encoding="utf-8", errors="replace"))
            if r["error"]:
                bad.append("%s 解析失败：%s" % (r["file"], r["error"]))
            for h in r["hits"]:
                bad.append("%s:%d 用了未定义的名字 %s" % (r["file"], h["line"], h["name"]))
        self.assertEqual(bad, [], "tools/tests 出现未定义名：\n  " + "\n  ".join(bad))

    def test_scanner_itself_still_detects_a_planted_name(self):
        """变异自检：种一个未定义名，扫描器必须报出来。

        没有这条，上面的关卡哪天退化成"永远通过"也不会有人发现 —— 本工程为
        "一个永不失败的检查"吃过好几次亏（见 tools/verify_dmg_pyz.py 的同类教训）。
        """
        src = (
            "import os\n\n\n"
            "def guarded():\n"
            "    try:\n"
            "        return definitely_not_defined_xyz(1)\n"
            "    except Exception:\n"
            "        pass\n\n\n"
            "def ok():\n"
            "    return os.sep\n"
        )
        r = U.analyze("planted.py", src)
        names = [h["name"] for h in r["hits"]]
        self.assertIn("definitely_not_defined_xyz", names,
                      "扫描器没抓到故意种的未定义名 → 关卡已失效")
        self.assertNotIn("os", names, "把已导入的名字误报成未定义，会淹没真信号")

    def test_scanner_understands_common_binding_forms(self):
        """常见绑定形式不能误报（误报多了，真信号就没人看了）。"""
        src = (
            "import a.b as ab\n"
            "from typing import List\n"
            "CONST = 1\n"
            "PAIR: tuple = (1, 2)\n"
            "def f(x, *args, k=2, **kw):\n"
            "    for i in range(3):\n"
            "        with open('p') as fh:\n"
            "            data = [y * i for y in ab.values()]\n"
            "    try:\n"
            "        pass\n"
            "    except ValueError as e:\n"
            "        data = str(e)\n"
            "    global GLOBAL_WRITTEN\n"
            "    lambda z: z + CONST\n"
            "    return data, kw, List, fh, GLOBAL_WRITTEN\n"
        )
        r = U.analyze("forms.py", src)
        self.assertEqual([h["name"] for h in r["hits"]], [],
                         "误报清单：%s" % r["hits"])


class TestProductModulesStillParse(unittest.TestCase):
    """关卡的前提：产品模块都能被 AST 解析（解析失败会被静默跳过 = 漏网）。"""

    def test_all_parse(self):
        skipped = []
        for path in _product_files():
            try:
                ast.parse(path.read_text(encoding="utf-8", errors="replace"))
            except SyntaxError as exc:
                skipped.append("%s: %s" % (path.name, exc))
        self.assertEqual(skipped, [], "有产品模块解析失败，扫描关卡对它无效")


class TestGapClockClosesOnlyOnConfirmedHealth(SFCase):
    """空窗计时器只许在"这一拍体检全过"那条早退路上结表。

    为什么必须用位置断言（AGENTS §6 第 2 条要求写清"守的是什么、为什么不能用行为断言"）：
    要守的事实是**结表这件事发生在哪条支路**。写成行为断言就得把 `_force_top` 的纠正
    分支整条跑起来（setLevel_ / CATransaction / orderFront / 逐个属性写），那等于为了
    测一个位置，把被测逻辑连同原生窗口协议一起搬进测试里重抄一遍；而"把 `_note_gap_closed()`
    挪到纠正分支"这种改法在行为上依然能让所有可调用判据全绿 —— 实测种过这一处，
    tests/test_diagnostics_payload.py 当时一条都没红。
    时长被提前结掉的后果不是数字小一点，而是**方向相反**：一场始终没修好的空窗会被
    记成"某次纠正之后恢复了"，而用户看到的是一直只剩背景。
    """

    def test_single_close_site_inside_the_healthy_early_return(self):
        src = (pathlib.Path(__file__).resolve().parents[1] / "sf_glass.py").read_text(
            encoding="utf-8")
        lines = src.splitlines()
        closes = [i for i, line in enumerate(lines) if "_note_gap_closed()" in line]
        self.assertEqual(len(closes), 1,
                         "结表点必须只有一处，当前 %d 处：%s" % (
                             len(closes), [lines[i].strip() for i in closes]))
        window = "\n".join(lines[max(0, closes[0] - 8):closes[0]])
        self.assertIn("if not space_changed and fault is None:", window,
                      "唯一的结表点不在「体检全过」那条早退路上；"
                      "纠正动作之后不许结表，那时还没证明用户看得见内容")
        after = lines[closes[0] + 1].strip() if closes[0] + 1 < len(lines) else ""
        self.assertTrue(after.startswith("return"),
                        "结表之后必须立刻退出这一拍：继续往下走就是去做纠正动作，"
                        "而那一刻的健康状态还没有被证明过")

    def test_open_site_is_inside_the_rate_limited_retop_log(self):
        """起表必须长在 `_log_retop` 里 —— 那才是"检出一条失效"的唯一入口。

        写在别处（例如每次 `orderFront`）会把「我动了手」当成「出事了」，计时器于是
        永远在开合之间抖动，量不出任何一场真实的空窗。
        """
        src = (pathlib.Path(__file__).resolve().parents[1] / "sf_glass.py").read_text(
            encoding="utf-8")
        lines = src.splitlines()
        opens = [i for i, line in enumerate(lines) if "_note_gap_open(" in line
                 and not line.strip().startswith("def ")]
        self.assertEqual(len(opens), 1, "起表点必须只有一处")
        # 从起表那一行**往上找最近的 def**，而不是取固定行数的窗口：
        # `_log_retop` 的 docstring 有二十几行，窗口一窄就会看漏（本条判据第一次就
        # 是这样误红的 —— 锁位置的工具自己得先站得住）。
        enclosing = next((lines[j].strip() for j in range(opens[0] - 1, -1, -1)
                          if lines[j].lstrip().startswith("def ")), "")
        self.assertIn("def _log_retop", enclosing,
                      "起表没跟着留痕走：那条失效不会有时长记录")


class TestNoNewSilentSwallowOnHotPaths(unittest.TestCase):
    """棘轮：热路径/功能入口上"纯 `except …: pass`"的数量只许降、不许升。

    为什么不是"全部清零"：2026-09-24 盘点产品里有 206 处静默兜异常，其中不少是
    **刻意**的（关窗时 orderOut、`queue.Empty` 这类正常控制流、绘制安全网已有自己的
    日志）。v1.4.30 已给 28 处"失败=用户看得见功能没了"的站点接上
    `sf_logging.log_swallow`（日志里固定带 SWALLOWED 前缀，一句 grep 就能问
    "现在还有没有功能在静默失效"）。本关卡守的是**别再新增盲区**。

    下界怎么降：把某个函数里的 `pass` 换成 `log_swallow(...)` 后，同步把
    BASELINE 调小；只降不升。
    """

    HOT = ("mouse", "key", "paint", "wheel", "enter", "leave", "context", "drop",
           "_anim_tick", "_poll", "_dot_tick", "_flash", "_rainbow", "_force_top",
           "_reassert_top", "_sync_glass", "_set_glass", "_glass_mask",
           "_snap_to_edge", "_toggle_chart", "_open_", "_close_", "_apply_quote",
           "_show_", "_start_", "_raise_", "_place_panel", "_move_near",
           "_restore_window", "_minimize", "_quit", "_save_", "_after_save",
           "_editor", "_panel", "_update_target", "_auto_width", "_fetch_")
    BASELINE = 21

    def _count(self):
        n = 0
        detail = []
        for path in _product_files():
            try:
                tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
            except SyntaxError:
                continue
            for fn in ast.walk(tree):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if not fn.name.startswith(self.HOT):
                    continue
                for node in ast.walk(fn):
                    if isinstance(node, ast.ExceptHandler) and len(node.body) == 1 \
                            and isinstance(node.body[0], ast.Pass):
                        n += 1
                        detail.append("%s:%d %s()" % (path.name, node.lineno, fn.name))
        return n, detail

    def test_hot_path_silence_does_not_grow(self):
        n, detail = self._count()
        self.assertLessEqual(
            n, self.BASELINE,
            "热路径新增静默兜异常（会把 NameError 一起吞掉，功能可永久静默失效）。"
            "请改用 sf_logging.log_swallow(模块.函数, exc)：\n  "
            + "\n  ".join(detail))

    def test_log_swallow_is_greppable(self):
        """日志前缀是这条链路的**唯一用法**：grep SWALLOWED 就能问"还有没有功能在静默失效"。"""
        import sf_logging
        import tempfile
        tmp = pathlib.Path(tempfile.mkdtemp()) / "swallow.log"
        old = sf_logging.LOG_PATH
        try:
            sf_logging.LOG_PATH = str(tmp)
            sf_logging.reset_log_throttle()
            sf_logging.log_swallow("mod.func", NameError("name '_objc_deps' is not defined"))
        finally:
            sf_logging.LOG_PATH = old
        text = tmp.read_text(encoding="utf-8")
        self.assertIn("SWALLOWED mod.func", text)
        self.assertIn("_objc_deps", text)


class TestNoNewSilentSwallowAnywhere(unittest.TestCase):
    """棘轮：**全量**（含非热路径）"纯 `except …: pass`"只降不升。

    为什么在热路径白名单之外还要再加一条：热路径那份 HOT 前缀清单只盖住了
    49 处里的 21 处，剩下的都在它管不到的地方。而"新增盲区"恰恰是从这些
    没人盯的函数里长出来的 —— 本仓为 `_objc_deps`（工具窗抬层）、`bottom_rows`
    （底部宽度自适应）各潜伏了几十个版本，症状都是"功能一次也没生效过"。

    基线 179 = 2026-09-24 用 AST 实测的产品模块总数（`len(body)==1 and Pass`）。
    这不表示 179 处都合理：清理类（关窗 orderOut）、正常控制流（queue.Empty）、
    已有专用日志/计数器的可以留着，但**新增一条就得先说清为什么**。
    下界怎么降：换成 `sf_logging.log_swallow(模块.函数, exc)` 后同步调小 BASELINE。

    178 → 179 这一条是谁、为什么（v1.4.33 加 last_failures 摘要时）：
    本关卡上线当天就逮到新增日志设施自己写了 3 处纯静默兜异常 —— 已按规矩改成
    `sf_logging.internal_faults()` 计数器留痕，只剩 `sf_logging._note_internal_failure`
    自己的那一处：它是"日志子系统本身坏了"时的最后兜底，除了忽略别无可为
    （计数都写不进去，说明解释器正在退出）。这条债务刻意记账在这里，而不是偷偷吸收。
    """

    # 按文件记，不给全仓总数（复核第 9 项改的）：
    # 只有一个总数基线时，stock_float_window 那 76 处会替所有文件挡额度 ——
    # 在 target_panel 新加一处静默兜异常，总数上完全看不出来。
    BASELINE = {
        "sf_bootstrap.py": 2, "sf_calendar.py": 2, "sf_config.py": 2,
        "sf_editor.py": 8, "sf_glass.py": 8, "sf_icons.py": 3,
        "sf_logging.py": 17, "sf_network.py": 7, "sf_notify.py": 3,
        "sf_quote.py": 7, "sf_runtime.py": 0, "sf_system_alert.py": 4,
        "sf_temp_sweep.py": 1, "sf_ui_render.py": 5, "sf_universe.py": 3,
        "sf_update.py": 2, "stock_float_window.py": 75, "target_panel.py": 5,
    }

    def _count(self):
        """返回 ({文件名: 纯 except…pass 条数}, 明细行)。"""
        per = {}
        detail = []
        for path in _product_files():
            try:
                tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
            except SyntaxError:
                continue
            n = 0
            for node in ast.walk(tree):
                if isinstance(node, ast.ExceptHandler) and len(node.body) == 1 \
                        and isinstance(node.body[0], ast.Pass):
                    n += 1
                    detail.append("%s:%d" % (path.name, node.lineno))
            per[path.name] = n
        return per, sorted(detail)

    def test_total_silence_does_not_grow(self):
        per, detail = self._count()
        worse = []
        for fname, n in sorted(per.items()):
            cap = self.BASELINE.get(fname, 0)
            if n > cap:
                worse.append("%s：基线 %d，实际 %d（新增 %d 处）"
                             % (fname, cap, n, n - cap))
        self.assertEqual(
            worse, [],
            "产品模块新增静默兜异常（按文件判，只降不升）。\n  "
            + "\n  ".join(worse)
            + "\n新增站点：" + ", ".join(detail[-12:])
            + "\n要么改成 sf_logging.log_swallow(模块.函数, exc)，要么在提交说明里"
              "写清为什么这里静默是安全的，再同步调整 BASELINE。")

    def test_baseline_is_not_comfortably_stale(self):
        """基线离实际太远时要提醒收紧：留着大额度 = 下次新增没人报警。"""
        per, _ = self._count()
        slack = sum(cap - per.get(f, 0) for f, cap in self.BASELINE.items())
        self.assertLessEqual(
            slack, 10,
            "基线比实测宽 %d 处（各文件额度攒出来的余量）—— 请把 BASELINE 调到"
            "实测值，否则后面新增 10 处以内都不会红。" % slack)

class TestReleaseChainCallsTheGate(SFCase):
    """发版链必须真的调用回归关卡，而且必须排在**不可逆动作之前**（v1.4.59 补，S7-②）。

    为什么这里允许读源码文本（AGENTS §6 第 2 条平时禁止）：守的对象是两个 bash 脚本的
    **调用与顺序**，不是任何 Python 可调用行为。把它写成行为断言只能真去跑一次发版
    （覆盖桌面 DMG、替换 /Applications、切线上 OTA 指针），代价不可接受。
    因此断言只挑**命令 token**（不挑注释措辞），改脚本里的措辞不会误报；而它挑的是
    顺序这一件真事：30 轮走查实测 build_release.sh 从头到尾没调用过 run_tests.sh，
    所有核验都排在打包之后 —— 排错位置的关卡等于没有关卡。
    """

    @staticmethod
    def _text(name):
        return (ROOT / name).read_text(encoding="utf-8")

    def test_build_release_checks_stamp_before_any_irreversible_step(self):
        s = self._text("build_release.sh")
        gate = s.find("gate_stamp.py check")
        self.assertGreater(gate, 0, "build_release.sh 不再核验关卡时间戳")
        for marker, what in (("hdiutil create", "生成 DMG"),
                             ('cp -R "$PROJ/dist/$APP_BUNDLE" "$APP_DST"', "替换 /Applications"),
                             ("publish_update_release.py", "切换线上 OTA 清单")):
            pos = s.find(marker)
            self.assertGreater(pos, 0, "找不到 %s，脚本结构变了，这条断言需要重新设计" % what)
            self.assertLess(gate, pos, "关卡核验排在 %s 之后 —— 那已经撤不回了" % what)

    def test_quality_record_is_bound_before_expensive_preflight(self):
        """质量记录缺失必须在公开历史/签名扫描前失败，避免无效重试浪费发布时间。"""
        source = self._text("build_release.sh")
        record_check = source.find('QUALITY_RECORD="${SF_QUALITY_RECORD:-}"')
        validate = source.find('tools/quality_evidence.py validate "$QUALITY_RECORD"')
        precheck = source.find('PRECHECK_DIR="$(mktemp -d')
        irreversible = source.find("publish_update_release.py")
        self.assertGreater(record_check, 0)
        self.assertGreater(validate, record_check)
        self.assertGreater(precheck, validate,
                           "质量记录结构必须在并行公开/签名前置核验前完成")
        self.assertGreater(irreversible, validate)
        self.assertIn("SF_QUALITY_RECORD", source[record_check:validate + 200])

    def test_binary_compatibility_gate_precedes_dmg_install_and_ota(self):
        # The contract is shell command order; testing execution would publish a real release.
        source = self._text("build_release.sh")
        gate = source.find('tools/verify_macos_compat.py "$PROJ/dist/$APP_BUNDLE" --runtime')
        self.assertGreater(gate, 0)
        for marker in ("hdiutil create", 'cp -R "$PROJ/dist/$APP_BUNDLE" "$APP_DST"',
                       "tools/publish_update_release.py --assets"):
            self.assertGreater(source.find(marker), gate)

    def test_card_corner_gate_is_mandatory_before_dmg_install_and_ota(self):
        # Only shell command order can be checked without publishing an actual release.
        source = self._text("build_release.sh")
        command = 'tools/verify_card_corners.py --app "$PROJ/dist/$APP_BUNDLE" --native'
        gate = source.find(command)
        self.assertGreater(gate, 0)
        self.assertIn('|| { echo "!! 卡片圆角门禁未过', source[gate:])
        self.assertNotIn("SKIP_TESTS", source[gate:source.find("# ---- 3.7)", gate)])
        for marker in ("hdiutil create", 'cp -R "$PROJ/dist/$APP_BUNDLE" "$APP_DST"',
                       "tools/publish_update_release.py --assets"):
            self.assertGreater(source.find(marker), gate)

    def test_build_release_skip_flag_prints_warning(self):
        """--skip-tests 必须大声警告，而且不许是唯一的分支。

        静默放行等于把「跑过回归」变成可选项；WARNING 一旦被挪到别处（比如只剩一句
        echo），这个开关就会变成常态通道。所以查的是**先后位置关系**：
        开关被解析 → 关卡分支读到它 → 打 WARNING → 仍保留核验路径 → 才进版本号。
        """
        s = self._text("build_release.sh")
        at = s.find("--skip-tests) SKIP_TESTS=1")
        guard = s.find('if [ "$SKIP_TESTS" -eq 1 ]')
        warn = s.find("WARNING", guard)
        check = s.find("gate_stamp.py check", warn)
        step1 = s.find("# ---- 1) 版本号")
        self.assertGreater(at, 0, "--skip-tests 开关不见了")
        self.assertGreater(guard, at, "跳过开关应当先被解析、再被关卡分支读到")
        self.assertGreater(warn, guard, "--skip-tests 必须大声警告")
        self.assertGreater(check, warn, "跳过分支之外仍要保留核验路径（不许只留放行）")
        self.assertGreater(step1, check, "关卡必须排在打包/发版动作之前")

    def test_run_tests_writes_stamp_only_after_full_green(self):
        s = self._text("run_tests.sh")
        success = s.find('echo "== 全部测试通过')
        write = s.find("gate_stamp.py write")
        self.assertGreater(success, 0, "成功行措辞变了，这条断言需要重新设计")
        self.assertGreater(write, success,
                           "关卡时间戳必须在「整轮全绿」之后才落，写在前面等于自证通过")

    def test_run_tests_writes_stamp_only_when_coverage_gate_ran(self):
        """跳过覆盖率门禁的那两档**不配**落关卡时间戳（v1.4.62 补的口子）。

        为什么读 shell 文本而不是跑行为：这条守的是"落戳的前提条件"，
        要行为验证就得真跑一整轮 run_tests.sh（几分钟）再去看戳，而
        `tests/test_run_tests_guard.py` 有那条 112 秒嵌套跑挂的前车之鉴。
        断言只挑命令 token（`COVERAGE_GATE_RAN`、`gate_stamp.py write`），
        不挑注释措辞 —— 改注释不会误报，去掉条件一定会报。

        2026-10-10 实测的形状：`SF_SKIP_COVERAGE=1` 那一轮根本没跑覆盖率断言，
        旧写法照样落戳，而 `build_release.sh` 第 0 步只看指纹同源就不补跑 ——
        于是一条环境变量静默绕过了覆盖率棘轮（那一轮全仓合并值 83.63%，
        戳上却写着"可用"）。不落戳才对：发版侧会因此自动补跑一整轮完整回归。
        """
        s = self._text("run_tests.sh")
        gate = s.find("tools/coverage_gate.py")
        self.assertGreater(gate, 0, "run_tests.sh 不再调用分档覆盖率门禁，这条判据需要重新设计")
        flag = s.find("COVERAGE_GATE_RAN=1")
        write = s.find("gate_stamp.py write")
        cond = s.find('COVERAGE_GATE_RAN" = "1')
        self.assertGreater(flag, 0, "置位标记不见了 —— 覆盖率分支不再声明自己跑过门禁")
        self.assertGreater(cond, 0, "落戳条件不见了，等于回到无条件落戳")
        self.assertLess(gate, flag, "置位必须排在门禁调用之后（先跑才算跑过）")
        self.assertLess(cond, write, "条件判断必须包住落戳那一行")
        self.assertEqual(s.count("COVERAGE_GATE_RAN=1"), 1,
                         "只许覆盖率分支置 1；多一处就说明某条跳过档也在自认跑过门禁")
        self.assertIn("不落关卡时间戳", s,
                      "跳过档必须打印「不落关卡时间戳」，否则人只会看见没戳而不知为何")

    def test_build_release_pushes_source_after_ota_and_confirms_by_readback(self):
        """发版链第 7 步：推源码必须在 OTA 之后，且以**回读归零**为准。

        为什么读 shell 文本（同本类开头那条的理由）：守的是"推不推、按什么顺序、
        怎么算已同步"这三个链路形状，行为验证只能真跑一次发版。
        只挑命令 token，不挑注释措辞。

        2026-10-10 实测的形状：origin 停在 v1.4.45 而本地 v1.4.61，16 个已发到用户手上的
        版本在 GitHub 上没有源码，而 workflow 只在 push 触发 —— 全仓搜 `git push` 当时
        命中 0 处。三个必须守住的点：
        ① 推的动作要存在（而不是"记得推"这种文档约定）；
        ② 排在 `publish_update_release.py`（切线上 OTA 指针）之后 —— 网络/权限问题
           不该让一次已完成的发版回滚；
        ③ 「已同步」的判据是回读 `rev-list --count origin/<branch>..HEAD` 归零，
           而不是 push 的退出码 —— push 返回 0 却推错分支时，旧写法会报成功。
        """
        s = self._text("build_release.sh")
        push = s.find('git -C "$PROJ" push -q origin "HEAD:refs/heads/$SRC_BRANCH"')
        ota = s.find("publish_update_release.py")
        readback = s.find('rev-list --count "origin/$SRC_BRANCH..HEAD"', push)
        self.assertGreater(push, 0, "build_release.sh 不再推源码 —— 第 7 步被删了")
        self.assertGreater(ota, 0, "找不到 OTA 发布调用，脚本结构变了，这条断言需要重新设计")
        self.assertGreater(push, ota, "推源码必须排在 OTA 之后：推失败不该让已完成的发版回滚")
        self.assertGreater(readback, push, "「已同步」必须以回读归零为准，不能只看 push 退出码")
        self.assertEqual(s.count("源码未同步"), 2,
                         "推失败与推错分支两条路都必须把「源码未同步」写进结尾结论，"
                         "少一处就是一条静默吞掉的失败")

    def test_gate_stamp_tool_ships_self_test(self):
        """闸门自带变异自检是仓内规矩（AGENTS §3.1）：没有它，某天判据退化成
        「永远 PASS」也不会有人发现。"""
        import gate_stamp as GS
        self.assertTrue(callable(getattr(GS, "self_test", None)),
                        "tools/gate_stamp.py 的 --self-test 被删了")
        self.assertEqual(GS.self_test(), 0,
                         "关卡时间戳的变异自检没过：它现在可能是一条永不失败的检查")


class TestStableSigningReleaseOrder(SFCase):
    """The irreversible release script cannot be executed as a unit-test fixture."""

    def test_identity_is_required_before_replacing_or_publishing_artifacts(self):
        text = (ROOT / "build_release.sh").read_text()
        preflight = text.index("tools/stable_signing.py preflight")
        move_old = text.index('mv "$PROJ/dist"')
        sign = text.index("tools/stable_signing.py sign")
        check = text.index("tools/stable_signing.py check")
        deliver = text.index("tools/release_feature_check.py")
        self.assertLess(preflight, move_old)
        self.assertLess(move_old, sign)
        self.assertLess(sign, check)
        self.assertLess(check, deliver)


class TestCISupplyChain(SFCase):
    def test_actions_are_pinned_and_dependency_audit_is_wired(self):
        # Remote Actions cannot run locally; lock the workflow's dependency boundary.
        import re
        workflow = (ROOT / ".github/workflows/python-ci.yml").read_text()
        actions = re.findall(r"^\s*- uses:\s+(\S+)", workflow, re.M)
        self.assertGreaterEqual(len(actions), 5)
        for action in actions:
            self.assertRegex(action, r"^actions/[a-z-]+@[0-9a-f]{40}$")
        self.assertIn(".venv/bin/python tools/dependency_audit.py", workflow)

    def test_cloud_regression_triggers_only_on_the_public_branch(self):
        """云端回归只在公开仓的 public-source 上跑，私有仓的开发分支不许再进触发名单。

        守的是**触发条件**而不是运行结果：远程 Actions 在本地跑不了（同类上一条），
        而这份文件被公开快照原样带走、两个仓库共用同一份。私有仓的 macOS 运行器
        按 10 倍费率折算，额度用完又没设消费额时，GitHub 不启动作业、
        只留一条 6 秒的 failure（2026-10-17 实测三连红），既烧额度又说不了任何事。
        谁把开发分支加回名单，这条必须当场红。
        """
        workflow = (ROOT / ".github/workflows/python-ci.yml").read_text()
        triggers = workflow.split("\non:", 1)[1].split("\njobs:", 1)[0]
        push_block = triggers.split("pull_request", 1)[0]
        pr_block = triggers.split("pull_request", 1)[1].split("workflow_dispatch", 1)[0]
        self.assertIn("public-source", push_block)
        self.assertIn("public-source", pr_block)
        self.assertIn("workflow_dispatch", triggers)
        for dev_branch in ("feat/target-price", "main", "master"):
            self.assertNotIn(
                dev_branch, push_block,
                "私有仓分支 %r 回到了 CI 的 push 名单：每次推送都会消耗 macOS 计费分钟，"
                "并在私有仓挂红叉" % dev_branch)


class TestTestWritingDiscipline(SFCase):
    """AGENTS §6 那 9 条书写纪律的自动关卡（v1.4.33 补：此前只有第 9、10 条有）。

    纪律没有关卡就等于没有 —— 存量 3000 多条违规**不批量改造**（AGENTS 明令），
    这里只保证不再变多。实现与判据在 tools/test_discipline_scan.py，
    基线是 tools/test_discipline_baseline.json。
    """

    def test_scanner_self_test_passes(self):
        """变异自检：R1~R7 各种一处违规，必须全部被抓到。

        这条是整道关卡的**可信度来源**：没有它，哪天判据退化成"永远 0 条"
        也不会有人发现（本仓为这类"永不失败的检查"付过太多次学费）。
        """
        import test_discipline_scan as TD
        self.assertEqual(TD.self_test(), 0,
                         "书写纪律扫描器自己不可信，它对存量数字说的都不算数")

    def test_no_new_discipline_violations(self):
        import test_discipline_scan as TD
        counts, errors = TD.scan(TD.ROOT / "tests")
        self.assertEqual(errors, [], "有测试文件解析失败，关卡对它无效：" + str(errors))
        try:
            baseline = TD.load_baseline()
        except (RuntimeError, ValueError) as exc:
            self.fail("基线不可用（**不算通过**）：%s" % exc)
        worse, _better = TD.diff(counts, baseline)
        self.assertEqual(
            worse, [],
            "新增违反 AGENTS §6 测试书写纪律的用例（棘轮只降不升）。\n  "
            + "\n  ".join(worse)
            + "\n各条判据见 tools/test_discipline_scan.py 的 docstring 与 AGENTS.md §6。")



class TestLocalReleaseBoundaries(SFCase):
    # This is a shell control-flow contract. Execute only the publication/final
    # blocks with a failing interpreter, never a real installer or publisher.
    def source(self):
        return (ROOT / "build_release.sh").read_text()

    def run_block(self, block, root, local, interpreter="/usr/bin/false"):
        import os
        import subprocess
        env = dict(os.environ, LOCAL_ONLY=str(local), PROJ=str(root), VERSION="fixture",
                   APP_BUNDLE="Skyrocket.app", DO_INSTALL="1", INSTALL_DIR=str(root / "app"),
                   VENV_PY=interpreter)
        return subprocess.run(["/bin/bash", "-c", "set -eu\n" + block], env=env,
                              capture_output=True, text=True, timeout=10)

    def test_local_mode_has_no_publication_side_effects_and_default_still_enters_publish(self):
        import tempfile
        source = self.source()
        start = source.index('if [ "$LOCAL_ONLY" -eq 0 ]; then\n# ---- 6)')
        end = source.index("# ---- 8)", start)
        with tempfile.TemporaryDirectory(prefix="sf_local_release_") as directory:
            root = pathlib.Path(directory)
            notes = root / "release_notes.txt"
            notes.write_text("fixture notes")
            local = self.run_block(source[start:end], root, 1)
            self.assertEqual(local.returncode, 0, local.stderr)
            self.assertEqual(list(root.iterdir()), [notes])
            public = self.run_block(source[start:end], root, 0)
            self.assertNotEqual(public.returncode, 0, "Default publication must reach its failing asset tool")

    def test_local_mode_still_fails_when_final_startup_check_fails(self):
        import tempfile
        source = self.source()
        block = source[source.index("# ---- 8)"):]
        with tempfile.TemporaryDirectory(prefix="sf_local_smoke_") as directory:
            root = pathlib.Path(directory)
            failed = self.run_block(block, root, 1)
            self.assertNotEqual(failed.returncode, 0)
            self.assertNotIn("本机覆盖完成", failed.stdout)
            passed = self.run_block(block, root, 1, "/usr/bin/true")
            self.assertEqual(passed.returncode, 0, passed.stderr)
            self.assertIn("本机覆盖完成", passed.stdout)

    def test_local_argument_does_not_enable_gate_or_build_skips(self):
        import subprocess
        source = self.source()
        start = source.index("\nDO_INSTALL=1\n")
        end = source.index("\nVENV_PY=", start)
        block = source[start:end] + '\nprintf "%s:%s:%s:%s" "$LOCAL_ONLY" "$SKIP_TESTS" "$DO_BUILD" "$DO_INSTALL"'
        for flags, expected in (([], "0:0:1:1"), (["--local-only"], "1:0:1:1")):
            import os
            env = dict(os.environ, SF_PUBLIC_REGRESSION="0")
            result = subprocess.run(["/bin/bash", "-c", block, "fixture", *flags],
                                    capture_output=True, text=True, timeout=10, env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, expected)


if __name__ == "__main__":
    unittest.main()


def _ceiling_line_problems(line):
    """真渲染那侧的上限公式该长什么样：必须对绝对上限与产品常量取小。缺任何一侧都算问题。"""
    problems = []
    if line is None:
        return ["找不到上限赋值（公式被挪走或整行删掉）"]
    if "min(" not in line:
        problems.append("上限不是取小 → 判据回到单向跟着常量走")
    if "GAP_CEILING_PX" not in line:
        problems.append("上限里没了绝对值 → 常量改大就能把松垮间距判成合理（台账 G07）")
    if "BOTTOM_ROW_GAP" not in line:
        problems.append("上限里没了产品常量 → 今天的收紧口径漂移")
    return problems


class TestBottomGapCeilingShape(SFCase):
    """像素关卡 `main()` 里那行上限公式的形状锁。

    为什么这里必须用源码形状锁而不是行为断言（§6 第 2 条要求写明守的是什么）：
    这条公式的唯一调用点在 `main()`，而 `main()` 要真建窗口离屏渲染才走到；
    `--self-test` 按它自己的契约**不许导入产品模块**（那条解耦本身是被验对象），
    所以自检永远看不见这一行 —— 行为断言在这个形状上拿不到输入。
    于是出现过这样一局：摘掉 `min(` 后全链路没有任何一条用例能变红。
    同族先例：§5.7 的「净空公式只许有一份实现」。
    """

    TOOL = "verify_bottom_gap_px.py"

    def _real_ceiling_line(self):
        import pathlib as pl
        import re
        src = (pl.Path(__file__).resolve().parent.parent / "tools" / self.TOOL).read_text()
        for line in src.splitlines():
            if re.search(r"^\s*ceiling\s*=\s*", line):
                return line
        return None

    def test_ceiling_line_keeps_both_sides_of_the_min(self):
        problems = _ceiling_line_problems(self._real_ceiling_line())
        self.assertEqual(problems, [], "上限公式放宽路径重新打开：" + "；".join(problems))

    def test_the_shape_lock_itself_can_fail(self):
        """三条放宽写法各演一遍，锁必须逐条抓到（不然它就是条永不失败的检查）。"""
        for bad in ("        ceiling = R.BOTTOM_ROW_GAP + 3",          # 摘掉 min：跟着常量跑
                    "        ceiling = GAP_CEILING_PX + R.BOTTOM_ROW_GAP",  # 绝对值被加成而不是取小
                    "        ceiling = max(GAP_CEILING_PX, R.BOTTOM_ROW_GAP + 3)",
                    None):
            with self.subTest(line=bad):
                self.assertTrue(_ceiling_line_problems(bad),
                                "这条锁抓不到该种的错：%r" % (bad,))
