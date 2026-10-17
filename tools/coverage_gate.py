#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""覆盖率门禁的分档版：产品代码一档，发版链上的 `tools/` 单独一档。

为什么要有这个文件（2026-10-10 实测逼出来的）
--------------------------------------------------------------------------
`run_tests.sh` 原来只有一条线：`coverage report --fail-under=84`，它算的是**全仓合并值**。
本轮全仓实测 83.63%，当场判红；拆开看却是两回事：

    产品代码（排除 tools/ 与 tests/）  10913 stmts  miss 1280  -> 88.27%
    发版链 tools/                       2561 stmts   miss  925  -> 63.88%

一条混合线把两种性质完全不同的东西压成同一个数字，而 AGENTS §6 第 10 条早就写过
那条记账口径：**链上核验器的判据大量靠子进程跑**（`--self-test`、
`tests/test_release_gates.py` 按 build_release.sh / run_tests.sh 现扫再 `subprocess` 起），
子进程不计入 `.coverage`。于是 `tools/` 里那 925 行 miss 里，有一大块是
"测了，但 coverage.py 看不见"。

后果是可预见的：下一轮看到"83% 红"的人会去做一件错事 —— 给已经守住的代码
再补一遍主进程用例，只为把账面数字抬上去；或者更糟，直接把 84 改成 83。
两个选择都比不过"把线画在有意义的位置"。

于是这里定两档，方向不一样：
  · 产品档 **只许升**（下限就是基线里的 `product_floor`，2026-10-13 起是 89；
    抬它要先把覆盖率真跑上去，别先改数字。这句话里的数字一律从基线取，不写死）
  · 链上档 **只降不升**（基线记当前实测向下取整，它守的是"别新增无人核验的链上代码"，
    不是"漂亮"；和 `tools/static_gate_baseline.json` 同一个思路）

两道红线是从本仓反复栽过的地方抄来的，别放宽：
  · 没有 `.coverage`、或某一分档一个文件都没配上 → **判红**。
    `coverage json` 对空数据照样吐一个 totals，而"空输入 = 整关通过"是
    `tools/test_discipline_scan.py` 的 `min_files` 那条红线专门对付的形状。
  · `--update` 只许把 floor 往上抬；要下调必须显式 `--allow-drop` 并当场大声 WARNING
    （与 `static_gate.write_baseline` 的逐键比较、`build_release --skip-tests` 同口径）。

用法
--------------------------------------------------------------------------
    ./.venv/bin/python tools/coverage_gate.py                 # 分档断言（run_tests.sh 用）
    ./.venv/bin/python tools/coverage_gate.py --report        # 看两档明细与最差文件
    ./.venv/bin/python tools/coverage_gate.py --update        # 链上档实测涨了才抬基线
    ./.venv/bin/python tools/coverage_gate.py --self-test     # 变异自检（发版链要求）

⚠ 参数形状的规矩（AGENTS §3.1，v1.4.61 补）：这个文件在发版链上，
`tests/test_release_gates.py::test_every_chain_tool_can_prove_it_fails` 会拿
`--self-test` 起子进程跑它，**rc==2 才算"没有可用自检"**。所以未知参数一律退 2，
绝不许落到默认动作里。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "tools" / "coverage_gate_baseline.json"

# 分档方式：链上一律按目录判，别按"文件名像不像工具"猜。
CHAIN_PREFIX = "tools/"
# tests/ 由 .coveragerc 的 omit 排除，这里再兜一次：万一 omit 被改掉，
# 用例代码混进产品档会让产品覆盖率虚高（判据跟着源码走，正是本仓忌的那族自指）。
EXCLUDE_PREFIX = ("tests/", "backups/", "build/", "dist/")

DEFAULT_DATA_FILE = ".coverage"
REQUIRED_MODULES = (
    "stock_float_window.py", "sf_quote.py", "sf_presence.py", "sf_glass.py",
    "sf_update.py", "targets.py", "sf_config.py",
)


def validate_module_floors(floors) -> None:
    """模块范围不可为空或缺项，bool 也不是合法百分比下限。"""
    if not isinstance(floors, dict) or not set(REQUIRED_MODULES).issubset(floors):
        raise RuntimeError("module_floors 必须包含全部七个必需模块")
    for name, floor in floors.items():
        if not isinstance(name, str) or type(floor) is not int or not 0 < floor <= 100:
            raise RuntimeError("module_floors 不合法：%r=%r（需要 1..100 整数）" % (name, floor))


def judge_modules(per_file, floors) -> tuple[int, list[str]]:
    """独立纯函数；总档达标不能掩盖单模块退化或范围缺失。"""
    validate_module_floors(floors)
    rows = {}
    for name, stmts, miss in per_file:
        if name in rows:
            raise RuntimeError("模块覆盖率重复：%s" % name)
        rows[name] = (stmts, miss)
    rc, lines = 0, []
    for name, floor in floors.items():
        if name not in rows:
            lines.append("!! 模块 %s 缺少 coverage 数据（范围失效）" % name)
            rc = 1
            continue
        stmts, miss = rows[name]
        if (type(stmts) is not int or type(miss) is not int
                or stmts <= 0 or not 0 <= miss <= stmts):
            lines.append("!! 模块 %s 语句数为零或统计不合法，无法给结论" % name)
            rc = 1
            continue
        pct = _percent(stmts, miss)
        if pct + 1e-9 < floor:
            lines.append("!! 模块 %s %.2f%% < 下限 %d%%（stmts %d，未覆盖 %d）"
                         % (name, pct, floor, stmts, miss))
            rc = 1
        else:
            lines.append("   [OK] 模块 %s %.2f%% ≥ %d%%（stmts %d，未覆盖 %d）"
                         % (name, pct, floor, stmts, miss))
    return rc, lines

# 2026-10-13 删掉了这里的 DEFAULT_PRODUCT_MIN = 84：全仓没有任何一处读它（grep 只命中定义那一行），
# 而产品档下限唯一真相是 tools/coverage_gate_baseline.json（load_baseline 在基线缺失时直接抛错，
# 从不回落到默认值）。留一枚没人读的 84，就等于在基线抬到 89 之后继续对下一读者说"下限是 84"。


def _percent(statements: int, missing: int) -> float:
    if statements <= 0:
        return -1.0
    return 100.0 * (statements - missing) / statements


def read_totals(data_file: str | None = None) -> dict:
    """从 coverage 数据里取每个文件的 stmts/missing。

    走 `coverage json` 这个公开接口而不是 `_analyze()`：后者是私有 API，
    升 coverage.py 版本时不会通知我们（本轮实测 version 7.16.1）。
    """
    exe = sys.executable
    env = dict(os.environ)
    if data_file:
        env["COVERAGE_FILE"] = data_file
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "coverage.json"
        proc = subprocess.run([exe, "-m", "coverage", "json", "-o", str(out), "-q"],
                              cwd=str(ROOT), env=env, text=True, capture_output=True)
        if proc.returncode not in (0, 1, 2) or not out.is_file():
            raise RuntimeError("coverage json 跑不出来（rc=%s）：%s%s"
                               % (proc.returncode, proc.stdout[-300:], proc.stderr[-300:]))
        report = json.loads(out.read_text(encoding="utf-8"))
    files = report.get("files", {})
    tiers = {"product": [0, 0, 0], "chain": [0, 0, 0]}   # stmts, missing, 文件数
    per_file = []
    for name, entry in files.items():
        norm = str(name).replace("\\", "/")
        if norm.startswith(EXCLUDE_PREFIX):
            continue
        s = entry.get("summary", {})
        stmts = int(s.get("num_statements", 0))
        miss = int(s.get("missing_lines", 0))
        key = "chain" if norm.startswith(CHAIN_PREFIX) else "product"
        tiers[key][0] += stmts
        tiers[key][1] += miss
        tiers[key][2] += 1
        per_file.append((norm, stmts, miss))
    return {"tiers": tiers, "files": per_file,
            "total_display": report.get("totals", {}).get("percent_covered_display")}


def load_baseline(path: Path = BASELINE) -> dict:
    """基线缺失或形状不对一律抛错 —— 静默用默认值等于给链条发免罪券。"""
    if not path.is_file():
        raise RuntimeError("覆盖率分档基线不存在：%s（必须入库）" % path)
    data = json.loads(path.read_text(encoding="utf-8"))
    for key in ("product_floor", "chain_floor"):
        if not isinstance(data.get(key), int) or data[key] <= 0:
            raise RuntimeError("基线里 %s 缺失或不是正整数：%r" % (key, data.get(key)))
    validate_module_floors(data.get("module_floors"))
    return data


def judge(tiers: dict, product_floor: int, chain_floor: int) -> tuple[int, list[str]]:
    """纯函数：喂进两档的数字，给结论。自检就是拿合成数字喂它。

    返回 (退出码, 逐行结论文本)。
    """
    lines: list[str] = []
    rc = 0
    for key, floor, label in (("product", product_floor, "产品代码"),
                              ("chain", chain_floor, "发版链 tools/")):
        stmts, miss, nfiles = tiers.get(key, [0, 0, 0])
        pct = _percent(stmts, miss)
        if nfiles == 0:
            lines.append("!! %s 一档一个文件都没配上（分档口径失效 / 没有 coverage 数据）"
                         % label)
            rc = 1
            continue
        if stmts == 0:
            lines.append("!! %s 一档语句数为 0，无法给结论（不当作通过）" % label)
            rc = 1
            continue
        if pct + 1e-9 < floor:
            gap = floor - pct
            lines.append("!! %s 覆盖率 %.2f%% 低于下限 %d%%（差 %.2f 个百分点；"
                         "stmts %d，未覆盖 %d）" % (label, pct, floor, gap, stmts, miss))
            rc = 1
        else:
            lines.append("   [OK] %s %.2f%% ≥ %d%%（stmts %d，未覆盖 %d，%d 个文件）"
                         % (label, pct, floor, stmts, miss, nfiles))
    return rc, lines


def load_data() -> tuple[dict | None, str]:
    """给三个入口共用的取数：拿不到数据就给理由，不抛栈、更不返回"看起来像通过"的空档。

    `.coverage` 被 `coverage erase` 清掉（跑测试期间正是这个状态）、文件根本不存在、
    coverage 模块跑不动 —— 三种都在这里归成一句人话，调用侧统一 rc=2。
    """
    try:
        return read_totals(), ""
    except Exception as exc:                       # coverage 自己的异常形状不止一种
        return None, str(exc)


def cmd_report(baseline: dict) -> int:
    data, why = load_data()
    if data is None:
        print("!! 覆盖率数据不可用：%s" % why, file=sys.stderr)
        return 2
    tiers = data["tiers"]
    rc, lines = judge(tiers, baseline["product_floor"], baseline["chain_floor"])
    module_rc, module_lines = judge_modules(data["files"], baseline["module_floors"])
    rc = max(rc, module_rc)
    lines.extend(module_lines)
    print("== 覆盖率分档明细 ==")
    for line in lines:
        print(line)
    print("   全仓合并值（仅作参照，不再当闸门）：%s%%" % data.get("total_display"))
    chain = sorted((f for f in data["files"] if f[0].startswith(CHAIN_PREFIX)),
                   key=lambda r: -r[2])
    print("   链上未覆盖最多的 8 个（抬链上档从这里挑）：")
    for name, stmts, miss in chain[:8]:
        pct = _percent(stmts, miss)
        print("      %-44s %5d stmts  miss %4d  (%.0f%%)" % (name, stmts, miss, pct))
    print("   ⚠ 记账口径：子进程里跑的代码不计入 .coverage（AGENTS §6 第 10 条），"
          "链上档低不等于没测。")
    return rc


def decide_update(new_chain: int, old_chain: int, allow_drop: bool) -> tuple[bool, str]:
    """纯函数：链上档的下限能不能写回。抽出来是因为它必须能自证，
    而 `cmd_update` 要读 `.coverage` —— CI 上先跑自检、后跑覆盖率，
    自检不许依赖数据文件存在（同 judge() 的理由）。
    """
    if new_chain >= old_chain:
        return True, ""
    if allow_drop:
        return True, ("!! WARNING：正在**下调**链上档下限 %d%% -> %d%%，这是放宽闸门。"
                      % (old_chain, new_chain))
    return False, ("!! 链上档实测 %d%% 低于基线 %d%%：下调下限必须显式 --allow-drop。\n"
                   "   正确做法是补链上档的用例（把判据从子进程挪进主进程也算），不是改数字。"
                   % (new_chain, old_chain))


def cmd_update(baseline: dict, allow_drop: bool, baseline_path: Path = BASELINE) -> int:
    data, why = load_data()
    if data is None:
        print("!! 拒绝写基线：%s" % why, file=sys.stderr)
        return 2
    tiers = data["tiers"]
    ps, pm, pn = tiers["product"]
    cs, cm, cn = tiers["chain"]
    if pn == 0 or cn == 0:
        print("!! 有一档没配上文件，拒绝写基线（写进去就是把闸门清空）", file=sys.stderr)
        return 1
    new_chain = int(_percent(cs, cm) // 1)         # 向下取整，别把 0.9 算成 1
    old_chain = baseline["chain_floor"]
    ok, why = decide_update(new_chain, old_chain, allow_drop)
    if not ok:
        print(why, file=sys.stderr)
        return 1
    if why:
        print(why, file=sys.stderr)
    baseline = dict(baseline)
    baseline["chain_floor"] = new_chain
    baseline["measured_product"] = round(_percent(ps, pm), 2)
    baseline["measured_chain"] = round(_percent(cs, cm), 2)
    baseline["updated"] = dt.date.today().isoformat()   # 写死日期 = 读基线的人不知道它是哪天核的
    baseline["source"] = "tools/coverage_gate.py --update"
    baseline_path.write_text(json.dumps(baseline, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
    print("已写入 %s：product_floor=%d chain_floor=%d（实测 产品 %.2f%% / 链上 %.2f%%）"
          % (baseline_path, baseline["product_floor"], new_chain,
             _percent(ps, pm), _percent(cs, cm)))
    return 0


def _tier(**kw) -> dict:
    base = {"product": [0, 0, 0], "chain": [0, 0, 0]}
    base.update(kw)
    return base


def self_test() -> int:
    """变异自检：每一种"闸门该拒绝的输入"都必须真的被拒。

    空壳 flag 蒙不过去 —— 本仓栽过四次（台账 G01~G05），所以每条都喂真数据形状。
    全部走 judge()/参数层，不依赖本机 .coverage 是否存在（CI 上先跑自检再跑覆盖率）。
    """
    cases: list[tuple[str, bool]] = []
    baseline_bytes_before = BASELINE.read_bytes() if BASELINE.is_file() else None

    def check(name: str, ok: bool):
        cases.append((name, ok))

    # 1) 产品档低于下限必须红
    rc, _ = judge(_tier(product=(1000, 200, 12), chain=(100, 10, 3)), 84, 60)
    check("产品档 80% 低于下限 84% —— 必须判红", rc == 1)

    # 2) 链上档低于下限必须红（且不能因为产品档好而被掩盖）
    rc, _ = judge(_tier(product=(1000, 10, 12), chain=(200, 90, 5)), 84, 60)
    check("链上档 55% 低于下限 60% —— 必须判红", rc == 1)

    # 3) 两档都高必须绿
    rc, _ = judge(_tier(product=(1000, 10, 12), chain=(200, 10, 5)), 84, 60)
    check("两档都达标 —— 必须放行", rc == 0)

    # 4) 空数据不许当通过（coverage json 对空库照样吐 totals）
    rc, _ = judge(_tier(), 84, 60)
    check("没有任何 coverage 数据 —— 必须判红，不许空转通过", rc == 1)

    # 5) 只有一档配上（分档口径失效，例如 tools/ 被挪进子目录）
    rc, _ = judge(_tier(product=(1000, 10, 12)), 84, 60)
    check("链上一档一个文件都没配上 —— 必须判红（分档范围失效）", rc == 1)

    # 6) 语句数为 0 但有文件（畸形输入）
    rc, _ = judge(_tier(product=(0, 0, 3), chain=(100, 1, 3)), 84, 60)
    check("产品档语句数为 0 —— 必须判红而不是 100%", rc == 1)

    # 7) 刚好等于下限必须放行（别把边界做成永远红）
    rc, _ = judge(_tier(product=(100, 16, 4), chain=(100, 40, 4)), 84, 60)
    check("恰好等于下限 —— 必须放行", rc == 0)

    # 8) 基线缺 floor 键必须抛错（静默用默认值等于闸门清空）
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "b.json"
        p.write_text('{"schema": 1}', encoding="utf-8")
        try:
            load_baseline(p)
            ok8 = False
        except RuntimeError:
            ok8 = True
    check("基线里没有 product_floor/chain_floor —— 必须抛错而不是用默认值", ok8)

    # 9) 基线文件不存在必须抛错
    try:
        load_baseline(Path(tempfile.mkdtemp()) / "nope.json")
        ok9 = False
    except RuntimeError:
        ok9 = True
    check("基线文件不存在 —— 必须抛错", ok9)

    # 10) 写回侧：下调链上档下限必须显式开关（走纯函数，不读 .coverage，
    #     这样 CI 上"先自检后跑覆盖率"的顺序不会让它假红/假绿）
    ok10a, _ = decide_update(new_chain=63, old_chain=70, allow_drop=False)
    ok10b, why10b = decide_update(new_chain=63, old_chain=70, allow_drop=True)
    ok10c, _ = decide_update(new_chain=75, old_chain=70, allow_drop=False)
    check("实测 63% 低于基线 70% 且没给 --allow-drop —— 必须拒绝写回", not ok10a)
    check("同样下调但显式给了 --allow-drop —— 必须放行且大声 WARNING",
          ok10b and "WARNING" in why10b)
    check("实测抬高（70% -> 75%）—— 必须无需开关就放行", ok10c)

    # 11) 未知参数必须退 2（这条是整个自检里最贵的教训：v1.4.61 实测，
    #     不认 --self-test 的工具按"默认动作"处理，一次"自检"把真实对外文档重写了）
    ok11 = main(["coverage_gate.py", "--definitely-not-a-real-flag"]) == 2
    check("未知参数 —— 必须退 2 且不落到任何默认动作", ok11)
    # 另一半洞（F5）：旧写法只在带 -- 的参数里找未知的，`coverage_gate.py report`
    # 这种漏了横杠的拼法会安静跑一次分档判定并把"通过"打在终端上。
    ok11b = main(["coverage_gate.py", "report"]) == 2
    check("漏了横杠的参数（report）—— 同样必须退 2", ok11b)
    # 11c) --min 往下压必须被拒（走纯函数，不碰 .coverage —— CI 上根本没有那份数据）：
    #      下限 89 之后，"一条环境变量就能把刚收紧的棘轮按回去"正是 §2 那一族的形状。
    probe = {"product_floor": 89, "chain_floor": 78}
    ok11c = min_override(dict(probe), "1") == 2 and probe["product_floor"] == 89
    check("--min 低于基线产品档下限 —— 必须拒绝且不改动任何值", ok11c)
    # 11d) 另一半：往上加严必须生效，否则"这轮临时按 91 核一次"就做不了了
    raised = dict(probe)
    ok11d = min_override(raised, "91") == 0 and raised["product_floor"] == 91
    check("--min 高于基线 —— 必须放行并把值改上去（临时加严是这只开关的唯一用途）", ok11d)
    ok11e = min_override(dict(probe), "abc") == 2
    check("--min 不是整数 —— 必须退 2", ok11e)

    module_floors = dict.fromkeys(REQUIRED_MODULES, 90)
    module_rows = [(name, 100, 10) for name in REQUIRED_MODULES]
    check("模块恰好达标必须放行", judge_modules(module_rows, module_floors)[0] == 0)
    check("总档绿但单模块退化必须红",
          judge_modules([(REQUIRED_MODULES[0], 100, 11)] + module_rows[1:],
                        module_floors)[0] == 1)
    check("必需模块缺失必须红", judge_modules(module_rows[1:], module_floors)[0] == 1)
    check("模块零语句必须红",
          judge_modules([(REQUIRED_MODULES[0], 0, 0)] + module_rows[1:],
                        module_floors)[0] == 1)
    bad = [n for n, ok in cases if not ok]
    for n, ok in cases:
        print("   [%s] %s" % ("OK" if ok else "种错未被抓到", n))
    # 12) 自检全程只读：基线一个字节都没动过（不靠递归跑 main —— 那会自我调用）
    after = BASELINE.read_bytes() if BASELINE.is_file() else None
    ok12 = after == baseline_bytes_before
    print("   [%s] %s" % ("OK" if ok12 else "种错未被抓到",
                          "自检只读 —— 不许改基线、不许写文件"))
    if not ok12:
        bad.append("自检写了基线")
    total = len(cases) + 1
    print("== 覆盖率分档关卡自检：%d 项通过，%d 项失败 ==" % (total - len(bad), len(bad)))
    return 1 if bad else 0


USAGE = ("可用：--report / --update / --allow-drop / --self-test / "
         "--min=<产品档下限>（只能临时加严，低于基线一律拒）")


def min_override(baseline: dict, raw: str) -> int:
    """`--min=` 的参数层：只许在基线之上临时加严，返回错误码（0 = 已生效）。

    为什么不直接改 `baseline["product_floor"]` 了事：下限从 84 抬到 89 之后，
    "一条环境变量就能把刚收紧的棘轮按回去"正是 §2 里 SF_SKIP_COVERAGE 那一族的形状
    —— run_tests.sh 那侧只看退出码，照样会落关卡时间戳。
    拆成纯函数是为了让自检能直接喂样本，不依赖本机有没有 .coverage（CI 上就没有）。
    """
    try:
        want = int(raw)
    except ValueError:
        print("!! --min 需要整数：%s" % raw, file=sys.stderr)
        return 2
    floor = baseline["product_floor"]
    if want < floor:
        print("!! --min 只许抬高不许压低：基线 product_floor=%d，请求 %d。\n"
              "   确实要降下限，请改 tools/coverage_gate_baseline.json 并写明理由"
              "（那是一笔要被人 review 的改动），别用环境变量绕。" % (floor, want),
              file=sys.stderr)
        return 2
    baseline["product_floor"] = want
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv if argv is None else argv)
    flags = [a for a in argv[1:] if a.startswith("--")]
    # 未知参数一律退 2，且**不看前缀**（2026-10-12 全方位复核 F5）：旧写法只在
    # 带 -- 的参数里找未知的，于是 `coverage_gate.py report` 这种漏了两个横杠的
    # 拼法会安静滑进默认动作 —— 跑一次分档判定并把"通过"打在终端上。
    unknown = [a for a in argv[1:]
               if a not in ("--report", "--update", "--allow-drop", "--self-test")
               and not a.startswith("--min=")]
    if unknown:
        # 见自检第 11 项：未知参数绝不许滑进默认动作。
        print("!! 不认识参数：%s\n   %s" % (" ".join(unknown), USAGE), file=sys.stderr)
        return 2
    try:
        baseline = load_baseline()
    except RuntimeError as exc:
        print("!! 覆盖率分档基线不可用：%s" % exc, file=sys.stderr)
        return 2
    for a in flags:
        if a.startswith("--min="):
            rc_min = min_override(baseline, a.split("=", 1)[1])
            if rc_min:
                return rc_min
    if "--self-test" in flags:
        return self_test()
    if "--update" in flags:
        return cmd_update(baseline, allow_drop="--allow-drop" in flags)
    if "--report" in flags:
        return cmd_report(baseline)
    data, why = load_data()
    if data is None:
        print("!! 覆盖率数据不可用：%s\n   没有跑过 `coverage run`，或数据已被清 —— "
              "这不算通过。" % why, file=sys.stderr)
        return 2
    rc, lines = judge(data["tiers"],
                      baseline["product_floor"], baseline["chain_floor"])
    module_rc, module_lines = judge_modules(data["files"], baseline["module_floors"])
    rc = max(rc, module_rc)
    lines.extend(module_lines)
    print("== 覆盖率分档门禁（产品 ≥%d%%，链上 ≥%d%%）=="
          % (baseline["product_floor"], baseline["chain_floor"]))
    for line in lines:
        print(line)
    if rc == 0:
        print("   ⚠ 链上档里大量判据靠子进程跑、不计入 .coverage —— 这是记账口径不是没测"
              "（AGENTS §6 第 10 条）")
    else:
        # 这句话里的数字必须跟着基线走：下限 2026-10-13 从 84 抬到 89，
        # 写死"别把 84 改小"就等于对着一个已经不存在的数字讲话。
        print("   红的时候先分清是哪一档：产品档掉 = 真缺口；链上档掉 = 新增了没人核验的"
              "链上代码。别把 %d 改小，也别给已守住的代码重复补测。" % baseline["product_floor"],
              file=sys.stderr)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
