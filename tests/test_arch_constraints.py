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
        "sf_editor.py": 7, "sf_glass.py": 8, "sf_icons.py": 3,
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

    def test_diag_coverage_gate_gets_the_real_release_baseline(self):
        """3.9 诊断覆盖关必须拿到真实发布基线，不许按版本号推断区间（v1.4.101 之后补）。

        守的还是 shell 调用形状：这条闸门没有「跑一次就知道」的可调用行为（跑一次等于
        发一次版）。实测同一条命令不给基线退 1（点名一个本轮根本没碰的模块）、给上一版
        发布头退 0 —— 推断区间把上一版已发布的改动也算进「本轮」。一道会因输入取错而
        凭空喊红的关，下一轮就会被 `--skip` 掉，所以这里钉三件事：基线被要求、
        要求排在调用之前、传给了 `--baseline`，并且整段仍排在三个不可逆动作之前。
        """
        s = self._text("build_release.sh")
        guard = s.find('if [ -z "${SF_RELEASE_BASELINE:-}" ]')
        call = s.find("diag_coverage_check.py --version")
        self.assertGreater(guard, 0, "3.9 没要求真实发布基线，会拿推断区间下结论")
        self.assertGreater(call, guard, "基线检查必须排在调用闸门之前（先拒绝，再决定跑不跑）")
        self.assertIn("--baseline \"$SF_RELEASE_BASELINE\"", s[call:call + 200],
                      "3.9 拿到基线却没传给闸门，等于白检查")
        for marker, what in (("hdiutil create", "生成 DMG"),
                             ('cp -R "$PROJ/dist/$APP_BUNDLE" "$APP_DST"', "替换 /Applications"),
                             ("tools/publish_update_release.py --assets", "切换线上 OTA 清单")):
            pos = s.find(marker)
            self.assertGreater(pos, call, "找不到 %s，脚本结构变了，这条断言需要重新设计" % what)

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

    def test_build_release_deploys_the_download_entry_before_rendering_its_public_command(self):
        """安装说明引用该入口时，部署与线上读回应是发版链的一部分。"""
        source = self._text("build_release.sh")
        ota = source.find("publish_update_release.py")
        deploy = source.find("node deploy-pages.mjs")
        guide = source.find('"$VENV_PY" tools/render_install_guide.py \\')
        self.assertGreater(deploy, ota, "下载入口必须在 OTA 资产出现后才部署")
        self.assertGreater(guide, deploy, "安装说明只能在入口部署并读回后生成")

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

    def run_block(self, block, root, local, interpreter="/usr/bin/false", extra_env=None):
        import os
        import subprocess
        env = dict(os.environ, LOCAL_ONLY=str(local), PROJ=str(root), VERSION="fixture",
                   APP_BUNDLE="Skyrocket.app", DO_INSTALL="1", INSTALL_DIR=str(root / "app"),
                   VENV_PY=interpreter)
        env.update(extra_env or {})
        # 被切出来的段里有分步计时的调用，函数定义在脚本头部，不在切片里：不先加载，
        # 公开模式会在 step_begin 处以 127 提前退出，"必须走到会失败的资产工具"就不再成立。
        timing = self.source().split("# BEGIN step_timing", 1)[1].split("# END step_timing", 1)[0]
        return subprocess.run(["/bin/bash", "-c", "set -eu\n" + timing + "\n" + block], env=env,
                              capture_output=True, text=True, timeout=10)

    def test_release_script_parses_before_any_release_action(self):
        """发版脚本有语法错误时，不能等到真发版才发现。"""
        import subprocess
        result = subprocess.run(["/bin/bash", "-n", str(ROOT / "build_release.sh")],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_local_install_copy_failure_restores_current_and_legacy_apps(self):
        """复制失败不能留下空的应用程序目录或两个只存在于 build/ 的旧包。"""
        import os
        import subprocess
        import tempfile
        source = self.source()
        start = source.index('# ---- 5.1) 安装到 /Applications（默认行为）----')
        end = source.index('\nif [ "$LOCAL_ONLY" -eq 0 ]; then', start)
        block = source[start:end]
        with tempfile.TemporaryDirectory(prefix="sf_release_restore_") as directory:
            root = pathlib.Path(directory)
            apps = root / "Applications"
            current = apps / "Skyrocket.app"
            legacy = apps / "实时股价-目标价版.app"
            for app, marker in ((current, "current"), (legacy, "legacy")):
                (app / "Contents").mkdir(parents=True)
                (app / "Contents" / "marker").write_text(marker, encoding="utf-8")
            (root / "dist" / "Skyrocket.app").mkdir(parents=True)
            (root / "build").mkdir()
            fake_bin = root / "bin"
            fake_bin.mkdir()
            fake_cp = fake_bin / "cp"
            fake_cp.write_text(
                "#!/bin/sh\nmkdir -p \"$3/Contents\"\nprintf partial > \"$3/Contents/marker\"\nexit 1\n",
                encoding="utf-8")
            fake_cp.chmod(0o755)
            env = dict(os.environ, PROJ=str(root), DO_INSTALL="1", LOCAL_ONLY="1",
                       INSTALL_DIR=str(current), APP_BUNDLE="Skyrocket.app", STAMP="fixture",
                       PATH=str(fake_bin) + ":" + os.environ.get("PATH", ""))
            result = subprocess.run(["/bin/bash", "-c", "set -eu\nstep_begin(){ :; }\n" + block],
                                    capture_output=True, text=True, env=env, timeout=20)
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual((current / "Contents" / "marker").read_text(encoding="utf-8"), "current",
                             "半成品移出后必须把原来的当前名称旧包恢复到安装路径")
            self.assertEqual((legacy / "Contents" / "marker").read_text(encoding="utf-8"), "legacy")
            failed = list((root / "build").glob("_failed_install_*"))
            self.assertEqual(len(failed), 1, "半成品必须移到 build，不能静默丢掉或留在应用程序")
            self.assertEqual((failed[0] / "Contents" / "marker").read_text(encoding="utf-8"), "partial")

    def test_local_install_copy_failure_keeps_backups_when_partial_app_cannot_move(self):
        """半成品若移不动，旧包宁可留在 build 回退区，也不能被嵌进坏目录。"""
        import os
        import subprocess
        import tempfile
        source = self.source()
        start = source.index('# ---- 5.1) 安装到 /Applications（默认行为）----')
        end = source.index('\nif [ "$LOCAL_ONLY" -eq 0 ]; then', start)
        block = source[start:end]
        with tempfile.TemporaryDirectory(prefix="sf_release_stuck_partial_") as directory:
            root = pathlib.Path(directory)
            apps = root / "Applications"
            current = apps / "Skyrocket.app"
            legacy = apps / "实时股价-目标价版.app"
            for app, marker in ((current, "current"), (legacy, "legacy")):
                (app / "Contents").mkdir(parents=True)
                (app / "Contents" / "marker").write_text(marker, encoding="utf-8")
            (root / "dist" / "Skyrocket.app").mkdir(parents=True)
            (root / "build").mkdir()
            fake_bin = root / "bin"
            fake_bin.mkdir()
            (fake_bin / "cp").write_text(
                "#!/bin/sh\nmkdir -p \"$3/Contents\"\nprintf partial > \"$3/Contents/marker\"\nexit 1\n",
                encoding="utf-8")
            (fake_bin / "mv").write_text(
                "#!/bin/sh\ncase \"$2\" in *'_failed_install_'*) exit 1 ;; esac\nexec /bin/mv \"$@\"\n",
                encoding="utf-8")
            for tool in (fake_bin / "cp", fake_bin / "mv"):
                tool.chmod(0o755)
            env = dict(os.environ, PROJ=str(root), DO_INSTALL="1", LOCAL_ONLY="1",
                       INSTALL_DIR=str(current), APP_BUNDLE="Skyrocket.app", STAMP="fixture",
                       PATH=str(fake_bin) + ":" + os.environ.get("PATH", ""))
            result = subprocess.run(["/bin/bash", "-c", "set -eu\nstep_begin(){ :; }\n" + block],
                                    capture_output=True, text=False, env=env, timeout=20)
            output = (result.stdout + result.stderr).decode("utf-8", errors="replace")
            self.assertNotEqual(result.returncode, 0, output)
            self.assertEqual((current / "Contents" / "marker").read_text(encoding="utf-8"), "partial")
            self.assertFalse((current / "Skyrocket.app").exists(),
                             "旧包被嵌入坏包目录却仍被误报为恢复")
            rollbacks = list((root / "build").glob("_rollback_*.app"))
            self.assertEqual(len(rollbacks), 2, "两代旧包都应作为可手动恢复的回退副本保留")

    def run_rollback_pruning(self, names):
        """真跑「回退副本回收」那一段：build/ 里放一批同名目录，mv 换成只记参数的桩。

        返回被点名要挪走的目录名（按挪走顺序）。桩不动文件，所以不会碰 /tmp，
        也不会真的回收谁。
        """
        import os
        import subprocess
        import tempfile
        source = self.source()
        start = source.index('stale_rollbacks="$(')
        end = source.index('else\n  echo "== 跳过 /Applications 安装', start)
        block = source[start:end]
        with tempfile.TemporaryDirectory(prefix="sf_release_prune_") as directory:
            root = pathlib.Path(directory)
            (root / "build").mkdir()
            for name in names:
                (root / "build" / name).mkdir()
            fake_bin = root / "bin"
            fake_bin.mkdir()
            moved_log = root / "moved.log"
            (fake_bin / "mv").write_text(
                "#!/bin/sh\nprintf '%s\\n' \"$1\" >> \"$MOVED_LOG\"\n", encoding="utf-8")
            (fake_bin / "mv").chmod(0o755)
            env = dict(os.environ, PROJ=str(root), STAMP="fixture", MOVED_LOG=str(moved_log),
                       PATH=str(fake_bin) + ":" + os.environ.get("PATH", ""))
            result = subprocess.run(["/bin/bash", "-c", "set -euo pipefail\n" + block],
                                    capture_output=True, text=False, env=env, timeout=20)
            output = (result.stdout + result.stderr).decode("utf-8", errors="replace")
            self.assertEqual(result.returncode, 0, output)
            if not moved_log.exists():
                return []
            return [pathlib.Path(line).name for line in
                    moved_log.read_text(encoding="utf-8").splitlines() if line]

    def test_rollback_pruning_keeps_two_newest_across_product_names(self):
        """两代产品名的回退副本一起按时间戳排：只留最新 2 份，其余都被点名回收。

        旧写法的第二个通配对不上改名前那一代的真实文件名（实时股价-目标价版），
        那一代的备份永远清不掉；且按整个文件名排序时产品名前缀会压过时间戳。
        """
        names = ["_rollback_Skyrocket_20261001_100000.app",
                 "_rollback_Skyrocket_20261003_100000.app",
                 "_rollback_Skyrocket_20261005_100000.app",
                 "_rollback_实时股价-目标价版_20260901_100000.app",
                 "_rollback_实时股价-目标价版_20261002_100000.app"]
        moved = self.run_rollback_pruning(names)
        self.assertEqual(moved, ["_rollback_实时股价-目标价版_20260901_100000.app",
                                 "_rollback_Skyrocket_20261001_100000.app",
                                 "_rollback_实时股价-目标价版_20261002_100000.app"])

    def test_rollback_pruning_ignores_directories_without_a_timestamp_suffix(self):
        """手工放进 build/ 的同前缀目录（名字不带时间戳）既不被回收，也不能扰乱真副本的排序。

        没有 15 位时间戳时，排序键会变成名字的末尾几个字母：有的排在最前被先挪走，
        有的排在数字之后被当成"最新"保留，反而把真正的旧副本挤出保留名额。
        """
        names = ["_rollback_manualcopy.app", "_rollback_foo.app",
                 "_rollback_Skyrocket_20261001_100000.app",
                 "_rollback_Skyrocket_20261002_100000.app",
                 "_rollback_Skyrocket_20261003_100000.app"]
        moved = self.run_rollback_pruning(names)
        self.assertEqual(moved, ["_rollback_Skyrocket_20261001_100000.app"])

    def test_rollback_pruning_with_nothing_to_prune_does_not_abort_the_release(self):
        """没有回退副本、或不超过 2 份时整段安静通过（脚本头部是 set -euo pipefail）。"""
        self.assertEqual(self.run_rollback_pruning([]), [])
        self.assertEqual(self.run_rollback_pruning(["_rollback_Skyrocket_20261001_100000.app",
                                                    "_rollback_Skyrocket_20261002_100000.app"]), [])

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
            self.assertNotIn("command not found", public.stderr,
                             "公开模式不是因为缺函数提前退出，而是真的走到了会失败的资产工具")

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

    def precheck_block(self):
        source = self.source()
        start = source.index('PRECHECK_DIR="$(mktemp')
        end = source.index('if [ "$LOCAL_ONLY" -eq 1 ]; then\n  "$VENV_PY" tools/render_install_guide.py --local-only', start)
        return "release_lock() { :; }\n" + source[start:end]

    def run_precheck(self, prepare_only, fail_export=False):
        """真跑前置核验那一段；解释器换成只记参数的桩，看它到底调用了哪些工具。"""
        import tempfile
        with tempfile.TemporaryDirectory(prefix="sf_precheck_") as directory:
            root = pathlib.Path(directory)
            log = root / "calls.log"
            stub = root / "stub.sh"
            stub.write_text(
                '#!/bin/bash\n'
                'echo "$*" >> "$STUB_LOG"\n'
                'if [ "${FAIL_EXPORT:-0}" = 1 ] && [ "$*" = "tools/public_release.py" ]; then\n'
                '  echo "stub: export refused"; exit 1\n'
                'fi\n'
                'exit 0\n')
            stub.chmod(0o755)
            done = self.run_block(self.precheck_block(), root, 0, str(stub), {
                "PREPARE_ONLY": str(prepare_only), "STUB_LOG": str(log), "TMPDIR": str(root),
                "FAIL_EXPORT": "1" if fail_export else "0"})
            calls = log.read_text().splitlines() if log.exists() else []
        return done, calls

    def test_prepare_only_also_runs_the_read_only_public_export_audit(self):
        # v1.4.105：公开源码审计到发布阶段才拒绝（注释里的示例邮箱），那时候选已冻结，
        # 只能退役整个候选重做。导出审计只读，所以 --prepare-only 与正式发布都要在这里跑。
        for prepare_only in (0, 1):
            with self.subTest(prepare_only=prepare_only):
                done, calls = self.run_precheck(prepare_only)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertIn("tools/public_release.py", calls)
                self.assertFalse([c for c in calls if "--publish" in c],
                                 "前置核验只许只读导出，不许带 --publish")

    def test_prepare_only_stops_when_the_public_export_audit_fails(self):
        for prepare_only in (0, 1):
            with self.subTest(prepare_only=prepare_only):
                done, calls = self.run_precheck(prepare_only, fail_export=True)
                self.assertNotEqual(done.returncode, 0)
                self.assertIn("发版前置核验未通过", done.stderr)
                self.assertIn("stub: export refused", done.stdout)

    def test_final_output_keeps_publication_and_acceptance_separate(self):
        source = self.source()
        self.assertIn("发布动作完成 v${VERSION}，发布后验收待完成", source)
        self.assertNotIn("发版完成 v${VERSION}（发布动作完成", source)

    def test_closing_line_the_script_really_prints_satisfies_the_post_release_checker(self):
        # 真跑收尾段、拿它实际打印的那一行去喂发布后验收的校验器，而不是各抄一份字面量：
        # 4874df1 改了脚本的收尾语、校验器没跟着改，夹具又各自抄着旧句子，于是一路全绿，
        # 直到发布后验收才被拒（隔了约 14 小时）。读脚本文本做字符串断言拦不住这种漂移。
        import tempfile
        from tools import quality_evidence as evidence
        block = self.source()[self.source().index("# ---- 8)"):]
        version = "9.9.9"
        app = pathlib.Path("/Applications/Skyrocket.app")
        env = {"VERSION": version, "SRC_BRANCH": "feat/fixture"}

        def missing(log):
            # 与 post_release_record 同一口径：清单取自 publish_log_markers，逐条要求出现在日志里。
            return [marker for marker in evidence.publish_log_markers(app, version) if marker not in log]

        with tempfile.TemporaryDirectory(prefix="sf_closing_line_") as directory:
            root = pathlib.Path(directory)

            def printed_log(script_block, sync_note):
                done = self.run_block(script_block, root, 0, "/usr/bin/true",
                                      dict(env, SYNC_NOTE=sync_note))
                self.assertEqual(done.returncode, 0, done.stderr)
                closing = [line for line in done.stdout.splitlines() if "完成 v" + version in line]
                self.assertEqual(len(closing), 1, done.stdout)
                others = [m for m in evidence.publish_log_markers(app, version)
                          if "完成 v" + version not in m]
                return "\n".join(others + closing)

            for sync_note in ("", "源码未同步"):
                with self.subTest(sync_note=sync_note):
                    log = printed_log(block, sync_note)
                    self.assertEqual(missing(log), [])
            # 反向对照：脚本把措辞改回旧说法时，这条判据必须当场红，否则它什么都没守。
            drifted = block.replace("发布动作完成 v${VERSION}", "发版完成 v${VERSION}")
            self.assertNotEqual(drifted, block)
            self.assertEqual(missing(printed_log(drifted, "")), ["发布动作完成 v" + version])
            # 「最终启动复验：」也是同一段打印的，同样拿真实输出对，而不是各抄一份。
            self.assertIn("最终启动复验：" + str(app), printed_log(block, ""))

    def test_local_only_log_cannot_pass_as_publish_evidence(self):
        # 本机覆盖模式也打印「已安装:」，只差发布那一半。它绝不能当成发布证据：
        # 否则一次本机重装就能生成 post_release.json。
        import tempfile
        from tools import quality_evidence as evidence
        block = self.source()[self.source().index("# ---- 8)"):]
        version = "9.9.9"
        app = pathlib.Path("/Applications/Skyrocket.app")
        with tempfile.TemporaryDirectory(prefix="sf_local_evidence_") as directory:
            done = self.run_block(block, pathlib.Path(directory), 1, "/usr/bin/true",
                                  {"VERSION": version, "SRC_BRANCH": "feat/fixture"})
            self.assertEqual(done.returncode, 0, done.stderr)
            log = done.stdout
            missing = [m for m in evidence.publish_log_markers(app, version) if m not in log]
            # 「已安装:」「版本:」两步不在这段切片里（它们要用真 .app 和 PlistBuddy），
            # 所以这里只钉住本机能判的差：发布资产行与收尾语都不该出现。
            self.assertIn("published_archive=", " ".join(missing))
            self.assertIn("发布动作完成 v" + version, missing)
            self.assertIn("本机覆盖完成 v" + version, log)
            self.assertNotIn("发布动作完成 v" + version, log)

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


class TestFeedbackWorkerAllowlistParity(SFCase):
    """客户端往诊断包里放的文件，服务端白名单必须认；两边的“可缺省”名单也必须一致。

    守的是跨语言契约：服务端是 JS，没有可调用的 Python 行为，只能读它的源码取名单。
    事故（2026-10-26）：1.4.104 起客户端固定带 feedback_ui.json，服务端白名单停在 10-20，
    每个完整反馈都被 422 diagnostic_member 拒收，用户只看到“上传失败”，两边各自的测试都是绿的。
    """

    @staticmethod
    def _worker_source():
        return (ROOT / "services" / "feedback-worker" / "src" / "index.mjs").read_text(encoding="utf-8")

    @classmethod
    def _worker_json_names(cls):
        import re
        block = re.search(r"const jsonNames = new Set\(\[(.*?)\]\);", cls._worker_source(), re.S)
        assert block, "服务端源码里找不到 jsonNames 白名单：解析失效，不是对账通过"
        return set(re.findall(r"'([a-z_]+\.json)'", block.group(1)))

    @classmethod
    def _worker_optional_names(cls):
        import re
        line = re.search(r"if \(\[([^\]]*)\]\.includes\(name\)\) continue;", cls._worker_source())
        assert line, "服务端源码里找不到可缺省名单：解析失效，不是对账通过"
        return set(re.findall(r"'([a-z_]+\.json)'", line.group(1)))

    def test_every_file_the_client_ships_is_accepted_by_the_worker(self):
        from sf_feedback_bundle import DIAGNOSTIC_JSON
        from sf_feedback_v3 import V3_DIAGNOSTIC_FILES
        missing = (set(DIAGNOSTIC_JSON) - set(V3_DIAGNOSTIC_FILES)) - self._worker_json_names()
        self.assertEqual(missing, set(), "客户端会带、服务端白名单不认（上传会被 422 拒收）：%s" % sorted(missing))

    def test_worker_does_not_accept_names_the_client_never_ships(self):
        from sf_feedback_bundle import DIAGNOSTIC_JSON
        from sf_feedback_v3 import V3_DIAGNOSTIC_FILES
        extra = self._worker_json_names() - (set(DIAGNOSTIC_JSON) - set(V3_DIAGNOSTIC_FILES))
        self.assertEqual(extra, set(), "服务端白名单多出客户端不认的名字：%s" % sorted(extra))

    def test_both_sides_agree_on_which_files_may_be_absent(self):
        from sf_feedback_bundle import OPTIONAL_DIAGNOSTICS
        self.assertEqual(self._worker_optional_names(), set(OPTIONAL_DIAGNOSTICS),
                         "两边对“可缺省文件”的名单不一致：旧版客户端的包会被一边接受、另一边拒收")

    def test_the_parser_reads_something_so_agreement_is_not_vacuous(self):
        self.assertGreaterEqual(len(self._worker_json_names()), 10)
        self.assertIn("feedback_ui.json", self._worker_json_names())

    @classmethod
    def _worker_rejections(cls):
        """服务端源码里每一处字面量 reject('码', 状态)：{码: 状态}（缺省状态 422，与 Rejection 的默认值一致）。"""
        import re
        found = {}
        for code, status in re.findall(r"reject\(\s*'([a-z_0-9]+)'\s*(?:,\s*(\d+))?\s*\)", cls._worker_source()):
            found[code] = int(status or 422)
        return found

    def test_the_client_tells_a_permanent_refusal_from_a_temporary_one_for_every_worker_code(self):
        # 事故形态（审查发现）：服务端新增或已有的拒绝码客户端认不出，用户被告知“稍后重试”，
        # 而同一份包永远过不了。这里按服务端实际会发的 (码, 状态) 逐个问客户端怎么归类。
        from sf_feedback_ui import _failure_kind
        rejections = self._worker_rejections()
        self.assertGreaterEqual(len(rejections), 60, "解析失效：读到的拒绝码太少，对账不能算通过")
        waiting = {"submission_in_progress", "lease_superseded"}
        # 这些是“服务端自己或上游出了问题”的 503，或请求太忙：客户端应当允许重试。
        for code, status in sorted(rejections.items()):
            kind = _failure_kind({"server_code": code, "http_status": status})
            if code in ("case_budget", "inbox_asset_capacity", "inbox_capacity"):
                self.assertEqual(kind, "full", code)
            elif code in ("identity_conflict", "remote_binding", "request_headers") or status in (413, 422):
                self.assertEqual(kind, "rejected", "%s(%s) 内容永远过不了，却被归成可重试" % (code, status))
            elif code in waiting or status in (400, 429, 503):
                self.assertEqual(kind, "retry", "%s(%s) 是暂时的，却被归成永久失败" % (code, status))
            else:
                self.fail("服务端的拒绝码 %s(%s) 没有归类预期，请决定它对用户算永久还是暂时" % (code, status))
