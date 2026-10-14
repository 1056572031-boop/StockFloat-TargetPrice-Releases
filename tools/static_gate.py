#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第一道防线：静态检查闸门（让写错的东西进不了仓库，而不是等运行时炸）。

用法：
    ./.venv/bin/python tools/static_gate.py              # 跑闸门（发版链/钩子/CI 用这个）
    ./.venv/bin/python tools/static_gate.py --update      # 把 B 级结果记为新基线
    ./.venv/bin/python tools/static_gate.py --self-test   # 变异自检：种错必须被抓到
    ./.venv/bin/python tools/static_gate.py --report      # 只打印分布，不做判定

为什么是"两级"而不是"严格类型化"（实测结论，2026-09-24）：
    本仓 15974 行产品代码里只有 10 个函数带注解（635 个函数，1.57%）。
    直接上 mypy --check-untyped-defs 报 **212 条**，随机抽查 8 条**全是误报或已有
    运行时兜底**：
      · `stock_float_window.py:819 connect 参数过多` —— PyQt 存根只声明了
        connect(callable)，运行时 connect(slot, Qt.QueuedConnection) 合法；
      · `stock_float_window.py:1728 addWidget 参数过多` —— 离屏实测 QMessageBox 的
        layout() 就是 QGridLayout，5 参重载合法；
      · `targets.py:82 float(a.get("target")) 可能为 None` —— 外面就套着
        `except (TypeError, ValueError): return False`，是刻意的脏配置防线。
    把 212 条一次性"改绿"既不现实也没收益。所以分两级：

    **A 级（硬零）**：只在"写错"时命中的规则，全仓含 tests/ 与 tools/，一条都不许有。
    **B 级（棘轮）**：存量有合理噪音的（未用导入、mypy 对 mixin 的误判），
        按 (工具, 文件, 错误码) 记条数，只许降不许升。

为什么 B 级基线按文件分开（而不是全仓一个总数）：
    `attr-defined` 高度集中在 mixin 栈 —— sf_ui_render 74 条、sf_glass 26 条，
    因为绘制逻辑写在 RenderMixin 里、属性却由宿主窗口赋值，静态分析看不到另一半。
    而主窗 stock_float_window 只有 2 条。混在一起记总数，等于用 74 条噪音给
    主窗当免罪额度；分开记，主窗再多一条**拼错的 self 属性**就会当场红。

守的是哪些真实事故（同族缺陷到 v1.4.29/v1.4.30 才被查出，都已潜伏几十个版本）：
    · `_objc_deps` 未导入就被 `except Exception: pass` 吞掉 → 工具窗抬层从未执行；
    · `bottom_rows` 未定义 → 底部两行宽度永不自适应；
    · `_chart_layer_key` 漏形参 → 缓存键恒错，核验器却报"36 组零差异"。
这三条的共同点是**运行时不会崩**（异常被兜住），只有静态层能拦住。

诚实的边界（别把这层当成万能）：
    · mypy 只跑产品根模块，不跑 tests/、tools/（噪音/收益不划算），那两处由
      ruff 的 A 级规则覆盖；
    · B 级基线内的存量债务本层**不管**，它只保证不再变多；
    · 检查器没装/跑挂了 = 闸门直接失败，绝不"跳过算通过"（本仓为"一个永不失败
      的检查"吃过太多次亏，见 tools/verify_dmg_pyz.py 的同款教训）。
"""
from __future__ import annotations

import argparse
import collections
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASELINE = pathlib.Path(__file__).with_name("static_gate_baseline.json")

PRODUCT_SKIP = {"run_tests.py", "setup.py"}

# 本次运行里出现的、名单之外的 mypy 错误族（供 main 打印提示）
NEW_MYPY_CODES: set[str] = set()

# ---- A 级：硬零，不需要基线，命中一条就红 ----
#   F821 未定义名 —— `_objc_deps`/`bottom_rows` 那一族：被 except 兜住后功能永久静默失效
#   F811 重复定义（覆盖同名）—— 重构时把前一个定义悄悄盖掉
#   F601 字典字面量重复键 / F632 用 is 比字面量 / F702 非法 continue 位置
#   B009 getattr(obj, "常量") —— 属性名写在字符串里，拼错时静态与运行时都发现不了，
#        只会静默走默认值，正是"某功能一次也没生效"的另一条通路
#   B010 setattr 同上
#   E9*  语法与缩进硬错（py_compile 之外的第二双眼）
RUFF_HARD = "F821,F811,F601,F632,F702,B009,B010,E9"
# ---- B 级：存量有合理噪音，进基线做棘轮（只降不升），只扫产品根模块 ----
#   F401 导入未用：产品里部分是刻意的再导出（如 stock_float_window 重导出
#        sf_config._migrate_legacy_config 供测试引用），不能一律判错；
#   F841 赋值未用：同上，偶有"仅为副作用求值"的写法。
RUFF_RATCHET = "F401,F841"
RUFF_EXCLUDE = ("backups", "build", "dist", ".venv", "node_modules", "__pycache__",
                ".private_tools", ".mypy_cache", ".ruff_cache")

# 交给 mypy 计账的错误码（B 级）。
# ⚠ 这里是**排除名单**而不是白名单（复核 F3 改的）：白名单会把没列进去的错误族
#   静默丢掉 —— `return-value`（返回值类型不对）、`dict-item`、`unreachable`
#   都是真会出 bug 的族，白名单下新增一条闸门照样绿，"新增即拦"就成了假话。
#   默认全计，只有**实测确认是存根噪音**的码才写进下面这份排除名单。
MYPY_CODES = ("attr-defined", "call-arg", "index", "operator", "no-redef",
              "name-defined", "arg-type", "assignment", "union-attr", "valid-type",
              "misc", "has-type", "var-annotated", "call-overload")
# 已确认不计账的噪音码（每条都要写清为什么，别顺手往里塞）：
#   import-not-found / module-not-found —— 第三方包缺存根时刷屏，与"写错代码"无关；
#   unused-ignore —— 本仓没用 type: ignore，出现即配置漂移，不是缺陷；
#   method-assign —— 探针/审计脚本的**本体**就是替换方法（frame_rate_audit 打桩
#       paintEvent、input_latency_probe 打桩事件链），这是设计而不是错误，
#       tools/ 里实测 25 条全部属于这一类。
MYPY_IGNORE_CODES = ("import-not-found", "module-not-found", "unused-ignore",
                     "method-assign")


def _ruff_exe() -> str:
    found = shutil.which("ruff")
    if found:
        return found
    local = ROOT / ".venv" / "bin" / "ruff"
    if local.exists():
        return str(local)
    raise RuntimeError("ruff 未安装。执行："
                       "uv pip install --python .venv/bin/python -r requirements.txt")


def _run(cmd: list[str], cwd: pathlib.Path | str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd or ROOT), capture_output=True, text=True)


def product_files() -> list[pathlib.Path]:
    return sorted(p for p in ROOT.glob("*.py") if p.name not in PRODUCT_SKIP)


def mypy_files() -> list[pathlib.Path]:
    """交给 mypy 计账的文件：产品根模块 + tools/。

    **不含 tests/（实测决定，v1.4.33）**：tests/ 下 617 条里约 352 条是
    `attr-defined`，来源是测试替身（`class _Fake(...)` 挂类属性、mock 出来的
    窗口对象），静态看永远"缺属性"，运行时全都成立 —— 把它记进基线等于给
    类型层灌一倍的纯噪音，反而盖住 tools/ 的真信号。
    为什么 tools/ 值得进来：历史上"核验器说假话""探针把累计值当单场景"
    全都出在 tools/，而它此前只被 ruff 的 A 级规则扫到（未定义名），
    属性拼错、None 解引用这类根本没进过视野。
    """
    return product_files() + sorted(p for p in (ROOT / "tools").glob("*.py")
                                    if p.name not in PRODUCT_SKIP)


def _parse_ruff(stdout: str) -> collections.Counter:
    """ruff concise 输出 -> 计数表，键为 'ruff:<相对路径>:<规则码>'。"""
    counts: collections.Counter = collections.Counter()
    for line in stdout.splitlines():
        head, _, rest = line.partition(": ")
        code = rest.split(" ", 1)[0].strip("[]* ")
        if not head or not code:
            continue
        counts["ruff:%s:%s" % (head.split(":", 1)[0], code)] += 1
    return counts


def run_ruff_hard() -> tuple[collections.Counter, collections.Counter]:
    """A 级：全仓（含 tests/ 与 tools/），期望 0。

    返回 (计数表, ruff 原始输出文本)：原文是给失败信息直接贴用的，
    调用方按 `hard, hard_raw = run_ruff_hard()` 取。
    """
    cmd = [_ruff_exe(), "check", "--no-cache", "--quiet", "--select", RUFF_HARD,
           "--output-format", "concise"]
    for d in RUFF_EXCLUDE:
        cmd += ["--exclude", d]
    cmd.append(".")                      # "." 已含 tools/ 与 tests/，不可重复列出
    cp = _run(cmd)
    if cp.returncode not in (0, 1):      # 1 = 有 finding，属正常输出
        raise RuntimeError("ruff（A 级）执行失败 exit=%d:\n%s%s"
                           % (cp.returncode, cp.stdout[-1500:], cp.stderr[-1500:]))
    return _parse_ruff(cp.stdout), cp.stdout


def run_ruff_ratchet() -> tuple[collections.Counter, str]:
    """B 级：只扫产品根模块的未用导入/未用赋值。"""
    files = [str(p.relative_to(ROOT)) for p in product_files()]
    cp = _run([_ruff_exe(), "check", "--no-cache", "--quiet",
               "--select", RUFF_RATCHET, "--output-format", "concise"] + files)
    if cp.returncode not in (0, 1):
        raise RuntimeError("ruff（B 级）执行失败 exit=%d:\n%s%s"
                           % (cp.returncode, cp.stdout[-1500:], cp.stderr[-1500:]))
    return _parse_ruff(cp.stdout), cp.stdout


def run_mypy() -> tuple[collections.Counter, str]:
    # 前置校验：`python -m mypy` 在**没装 mypy** 时的退出码恰好也是 1（"No module
    # named mypy"），而下面把 1 当作"有 finding"接受、stdout 又是空的 —— 于是整条
    # 类型层静默归零，B 级从 323 掉到 13，闸门照样打印"静态闸门通过"（实测）。
    # 本闸门自己的规矩：检查器跑不起来 = 直接失败，绝不提供"跳过算通过"。
    probe = _run([sys.executable, "-c", "import mypy.version"])
    if probe.returncode != 0:
        raise RuntimeError(
            "mypy 无法在本工程解释器里 import（exit=%d）：%s —— 类型层根本没跑，"
            "闸门拒绝把这算作通过" % (probe.returncode,
                                     (probe.stdout + probe.stderr).strip()[-200:]))
    cp = _run([sys.executable, "-m", "mypy", "--no-error-summary",
               "--check-untyped-defs", "--ignore-missing-imports",
               "--follow-imports=silent",
               "--exclude", r"^(backups|build|dist|node_modules|\.venv)/"]
              + [str(p.relative_to(ROOT)) for p in mypy_files()])
    if cp.returncode not in (0, 1):
        # 2 = 用法/配置错，3 = mypy 自身崩溃。一律**不算通过**：
        # 让闸门"静默降级成永不失败"正是本仓反复吃过的那类亏。
        raise RuntimeError("mypy 未能正常执行 exit=%d:\n%s"
                           % (cp.returncode, (cp.stdout + cp.stderr)[-2000:]))
    counts: collections.Counter = collections.Counter()
    for line in cp.stdout.splitlines():
        if " error: " not in line:
            continue
        fname = line.split(":", 1)[0]
        code = ""
        tail = line.rstrip()
        if tail.endswith("]") and "[" in tail:
            code = tail[tail.rfind("[") + 1:-1]
        if code in MYPY_IGNORE_CODES:
            continue                       # 已逐条确认过的存根噪音（见上面的名单注释）
        if code and code not in MYPY_CODES:
            # 新出现的错误族：照常计账，但**必须让人看见**，否则下一轮 --update
            # 会把它当既有债务无声吸收，闸门又一次说假话。
            NEW_MYPY_CODES.add(code)
        counts["mypy:%s:%s" % (fname, code or "uncoded")] += 1
    return counts, cp.stdout


# ------------------------------------------------------------- 判定 -----------

def collect() -> tuple[collections.Counter, collections.Counter, str]:
    """返回 (A 级命中, B 级计数, 说明文本)。"""
    # 前提校验：产品文件清单一旦因改名/路径变动而变空，B 级就会"零输入零错误"
    # 地无脑通过 —— 又是一个永不失败的检查等于没有检查。
    n = len(product_files())
    if n < 20:
        raise RuntimeError("产品根模块只取到 %d 个文件（预期 29 个量级），"
                           "扫描范围疑似失效，闸门拒绝下结论" % n)
    m = len(mypy_files())
    if m < n + 10:
        raise RuntimeError("mypy 目标只取到 %d 个文件（产品 %d + tools 应有 10 个以上），"
                           "tools/ 疑似没进类型层，闸门拒绝下结论" % (m, n))
    hard, hard_raw = run_ruff_hard()
    ratchet, _ = run_ruff_ratchet()
    mypy_counts, _ = run_mypy()
    if not mypy_counts:
        # 类型层一条 finding 都没有 = 两种可能，必须当场区分：
        #   ① 债务真的还清了（基线里 mypy:* 也应为 0，那时这里不该拦）；
        #   ② 这一层根本没执行（换了解释器、参数被改坏、mypy 缓存异常）。
        # 实测过 ②：桩掉 mypy 后 B 级 323→13，闸门打印"静态闸门通过"——
        # 一个永远通过的层比没有这层更危险，因为它会让人以为有层。
        base = load_baseline().get("counts", {}) if BASELINE.exists() else {}
        base_mypy = sum(int(v) for k, v in base.items() if str(k).startswith("mypy:"))
        if base_mypy:
            raise RuntimeError(
                "mypy 层本次产出 0 条 finding，而基线记有 %d 条 —— 类型层疑似未执行，"
                "闸门拒绝下结论（还清债务后请先把基线里的 mypy:* 一并降为 0）" % base_mypy)
    ratchet.update(mypy_counts)
    return hard, ratchet, hard_raw


def diff_against_baseline(counts: collections.Counter,
                         baseline: dict) -> tuple[list[str], list[str]]:
    """返回 (变差清单, 可下调清单)。变差 = 新出现的键 或 条数超过基线。"""
    old = baseline.get("counts", {})
    worse, better = [], []
    for key, n in sorted(counts.items()):
        cap = int(old.get(key, 0))
        if n > cap:
            worse.append("%s：%d → %d（新增 %d 条）" % (key, cap, n, n - cap))
    for key, cap in sorted(old.items()):
        n = int(counts.get(key, 0))
        if n < cap:
            better.append("%s：基线 %d，实际只剩 %d → 跑 `--update` 下调" % (key, cap, n))
    return worse, better


def load_baseline() -> dict:
    if not BASELINE.exists():
        raise RuntimeError("基线文件缺失：%s（用 --update 生成并一起提交）"
                           % BASELINE.relative_to(ROOT))
    data = json.loads(BASELINE.read_text(encoding="utf-8"))
    if not isinstance(data.get("counts"), dict):
        raise RuntimeError("基线缺少 counts 字段，格式已损坏：%s" % BASELINE)
    return data


def write_baseline(counts: collections.Counter, allow_rise: bool = False) -> None:
    # 棘轮的"棘轮"部分此前只存在于文档：`--update` 不与旧值比较，任何上调都被无条件
    # 写进新基线（git 历史实测 322→321→317→**323**，`df0bb0d` 把
    # mypy:sf_glass.py:attr-defined 从 22 抬到 28）。此后这条闸门只能守"别新增"，
    # 守不住"别把额度抬高"。现在：上调必须显式带 --allow-rise，且当场把差值打出来。
    if BASELINE.exists():
        old = load_baseline().get("counts", {}) or {}
        rises = ["  %s：%d → %d（+%d）" % (k, int(old.get(k, 0)), int(v),
                                          int(v) - int(old.get(k, 0)))
                 for k, v in sorted(counts.items()) if int(v) > int(old.get(k, 0))]
        if rises and not allow_rise:
            raise RuntimeError(
                "拒绝写基线：下列 %d 个键比现基线**更高**，棘轮只降不升。\n%s\n"
                "确认合理（例：升了 mypy/ruff 版本、或新错误族本就该记账）时，"
                "带 --allow-rise 重跑，并把理由写进提交说明。"
                % (len(rises), "\n".join(rises)))
    payload = collections.OrderedDict([
        ("_note", "第一道防线 B 级棘轮基线：静态检查条数只降不升，降了请同步 --update。"
                  "为什么分 A/B 两级、为什么按文件记，见 tools/static_gate.py 的 docstring。"),
        ("generator", "tools/static_gate.py --update"),
        ("counts", {k: int(v) for k, v in sorted(counts.items())}),
        ("total", int(sum(counts.values()))),
    ])
    BASELINE.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n",
                        encoding="utf-8")


# ---------------------------------------------------------- 变异自检 ----------

# A 级每条都要能抓到：抓不到就说明闸门已退化成"永远通过"。
PLANTED = [
    ("planted_undefined.py",
     "import os\n\n\ndef f():\n    return os.path.join(never_imported_xyz('a'))\n",
     "ruff:planted_undefined.py:F821"),
    ("planted_redef.py",
     "def helper():\n    return 1\n\n\ndef helper():\n    return 2\n",
     "ruff:planted_redef.py:F811"),
    ("planted_getattr.py",
     "class B:\n    pass\n\n\ndef use(b):\n    return getattr(b, \"nope\")\n\n\nuse(B())\n",
     "ruff:planted_getattr.py:B009"),
    # self 上拼错的属性：类体内 self 类型已知，mypy 能判 —— 正是历史上
    # `bottom_rows` / `_objc_deps` 那一类"运行时被兜住、功能永久没生效"的形态。
    ("planted_attr.py",
     "class A:\n    def __init__(self):\n        self.real = 1\n\n    def f(self)"
     ":\n        return self.typo_field\n",
     "mypy:planted_attr.py:attr-defined"),
]


def self_test(verbose: bool = False) -> int:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="sf_static_gate_selftest_"))
    bad = []
    try:
        for fname, src, expect in PLANTED:
            (tmp / fname).write_text(src, encoding="utf-8")
        found = set()
        try:
            cp = subprocess.run([_ruff_exe(), "check", "--no-cache", "--quiet",
                                 "--select", "%s,%s" % (RUFF_HARD, RUFF_RATCHET),
                                 "--output-format", "concise", "."],
                                capture_output=True, text=True, cwd=str(tmp))
        except RuntimeError as exc:              # 检查器没装
            print("!! 自检无法执行：%s" % exc)
            return 2
        for key in _parse_ruff(cp.stdout):
            found.add("ruff:" + key.split(":", 1)[1])
        cp = subprocess.run([sys.executable, "-m", "mypy", "--no-error-summary",
                             "--check-untyped-defs", "--ignore-missing-imports",
                             "--follow-imports=silent"]
                            # 必须逐个列文件名：subprocess 不过 shell，传 "*.py"
                            # 会被当成一个名叫 *.py 的目标，mypy 直接报缺目标，
                            # 于是自检"抓不到"—— 这类"检查器空转"正是本仓反复吃亏的地方。
                            + sorted(p.name for p in tmp.glob("*.py")),
                            capture_output=True, text=True, cwd=str(tmp))
        if cp.returncode > 2:
            print("!! 自检里的 mypy 未能执行（exit=%d）：\n%s"
                  % (cp.returncode, (cp.stdout + cp.stderr)[-800:]))
            return 2
        for line in cp.stdout.splitlines():
            if " error: " in line and line.rstrip().endswith("]"):
                tail = line.rstrip()
                found.add("mypy:%s:%s" % (line.split(":", 1)[0],
                                          tail[tail.rfind("[") + 1:-1]))
        for _, _, expect in PLANTED:
            if expect not in found:
                bad.append("种下的错误没被抓到：%s" % expect)
        # 反向自检：干净文件不能报（误报多了真信号就没人看）
        (tmp / "planted_undefined.py").write_text(
            "import os\n\n\ndef f():\n    return os.sep\n", encoding="utf-8")
        if True:
            cp = subprocess.run([_ruff_exe(), "check", "--no-cache", "--quiet",
                                 "--select", RUFF_HARD, "--output-format", "concise",
                                 "planted_undefined.py"],
                                capture_output=True, text=True, cwd=str(tmp))
            if cp.stdout.strip():
                bad.append("把已导入的名字误报成未定义，会淹没真信号：%s" % cp.stdout.strip())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if bad:
        print("!! 静态闸门自检失败（闸门本身不可信，测试结果不算数）：")
        for b in bad:
            print("   -", b)
        return 1
    print("   静态闸门自检通过：%d 类种子错误全部被抓到，干净文件零误报"
          % len(PLANTED))
    return 0


# ---------------------------------------------------------------- main --------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="第一道防线：静态检查闸门（A 级硬零 + B 级棘轮）")
    ap.add_argument("--update", action="store_true", help="把 B 级当前结果写为新基线")
    ap.add_argument("--allow-rise", action="store_true",
                    help="--update 时允许把某个键调高（默认拒绝；用了必须在提交说明里写为什么）")
    ap.add_argument("--self-test", action="store_true", help="变异自检")
    ap.add_argument("--report", action="store_true", help="只打印分布不判定")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    if args.self_test:
        return self_test()

    try:
        hard, ratchet, hard_raw = collect()
    except (RuntimeError, ValueError) as exc:
        print("!! 静态闸门无法执行（**不算通过**）：%s" % exc)
        return 2

    if args.report:
        print("-- A 级（必须为 0）--")
        for key, n in sorted(hard.items(), key=lambda kv: (-kv[1], kv[0])):
            print("%5d  %s" % (n, key))
        print("-- B 级（棘轮）--")
        for key, n in sorted(ratchet.items(), key=lambda kv: (-kv[1], kv[0])):
            print("%5d  %s" % (n, key))
        print("A 级 %d 条；B 级合计 %d 条 / %d 个组合"
              % (sum(hard.values()), sum(ratchet.values()), len(ratchet)))
        return 0

    if args.update:
        try:
            write_baseline(ratchet, allow_rise=args.allow_rise)
        except RuntimeError as exc:
            print("!! %s" % exc)
            return 1
        print("已写入 B 级基线：%d 条 / %d 个组合 → %s"
              % (sum(ratchet.values()), len(ratchet), BASELINE.relative_to(ROOT)))
        return 0

    baseline = load_baseline()
    worse, better = diff_against_baseline(ratchet, baseline)
    if not args.quiet:
        print("== 静态闸门：A 级（硬零）%d 条，B 级 %d 条（基线 %d 条）=="
              % (sum(hard.values()), sum(ratchet.values()), baseline.get("total", 0)))
    # 只对**基线里还没有**的错误族提示。否则会每次提交都刷一遍同样的话，
    # 刷十次就没人看了 —— 提示一旦变成背景噪声，真出现新族那天也没人注意。
    known = set()
    for key in baseline.get("counts", {}):
        if key.startswith("mypy:"):
            known.add(key.rsplit(":", 1)[1])
    fresh = sorted(NEW_MYPY_CODES - known)
    if fresh:
        print("   ⚠ 出现基线里没有的 mypy 错误族（已计入棘轮）：%s" % ", ".join(fresh))
        print("     这一族过去**完全不在闸门视野内**；确认要长期排除的，"
              "写进 MYPY_IGNORE_CODES 并注明理由。")
    rc = 0
    if hard:
        rc = 1
        print("!! A 级规则命中，这些**只在写错时出现**，必须清零后重试：")
        for line in hard_raw.splitlines():
            if line.strip():
                print("   -", line)
    if worse:
        rc = 1
        print("!! B 级出现**新增**错误（棘轮只降不升）：")
        for w in worse:
            print("   -", w)
        print("   先修掉；确认是误报（例：为宿主窗口赋值而写的 mixin 属性）才允许"
              " `--update`，并把基线改动与提交说明一起讲清为什么。")
    elif better and not args.quiet:
        print("   （存量已减少，建议下调基线）")
        for b in better:
            print("   -", b)
    if rc == 0 and not args.quiet:
        print("   静态闸门通过")
    return rc


if __name__ == "__main__":
    sys.exit(main())
