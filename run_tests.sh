#!/usr/bin/env bash
# 实时股价 发版前置测试关卡（v1.2.22 起提供，16.1 无 CI/CD 建议项的最小落地）
# 用法：在工程根目录执行  ./run_tests.sh
# 作用：一键跑通 tests/ 下全部单元/回归测试，任一项失败即非零退出，阻断发版。
set -euo pipefail
cd "$(dirname "$0")"

# 优先使用工程内 .venv（Python 3.14.7）解释器，避免误用系统解释器。
VENV_PY="$(pwd)/.venv/bin/python3"
if [ ! -x "$VENV_PY" ]; then
  echo "!! 未找到工程内虚拟环境解释器：.venv/bin/python3" >&2
  echo "!! 请先重建 .venv（Python 3.14.7），依赖见 requirements.txt，安装用 uv pip install：" >&2
  echo "!!   python3 -m pip install -r requirements.txt" >&2
  exit 1
fi
PY="$VENV_PY"
PUBLIC_REGRESSION="${SF_PUBLIC_REGRESSION:-0}"
if [ "$PUBLIC_REGRESSION" != "0" ] && [ "$PUBLIC_REGRESSION" != "1" ]; then
  echo "!! SF_PUBLIC_REGRESSION 只能是 0 或 1" >&2
  exit 2
fi
# 公开回归即使失败，也不能留下上一轮的发布资格；先保全旧戳，再运行任何测试。
if [ "$PUBLIC_REGRESSION" = "1" ] && [ -e build/.gate_stamp ]; then
  mkdir -p build/public-regression-invalidated || exit 1
  INVALIDATED_STAMP_DIR="$(mktemp -d "build/public-regression-invalidated/$(date +%Y%m%d-%H%M%S)-$$-XXXXXX")" || exit 1
  mv build/.gate_stamp "$INVALIDATED_STAMP_DIR/.gate_stamp" || exit 1
fi
echo "== 使用解释器: $PY ($("$PY" --version 2>&1)) =="

# 离屏渲染（默认）：测试全为单元/回归测试，不需要真实窗口。
# 背景：AGENTS.md 记载的规范命令是 `QT_QPA_PLATFORM=offscreen ./run_tests.sh`，
# 但 .git/hooks/pre-commit 直接调用本脚本时并未设置该变量 —— 钩子会走 cocoa 平台，
# 字体度量与 offscreen 不同，导致宽度/布局类断言失败并阻断提交（v1.2.63 提交时发现）。
# 这里在脚本内兜底默认 offscreen，使 钩子 / CI / 本地命令行 三种调用方式行为一致；
# 已显式设置时尊重原值，不影响手动指定其他平台插件。
export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-offscreen}"
echo "== Qt 平台插件: $QT_QPA_PLATFORM =="

# 用户数据红线：测试子进程虽已全部重定向落盘路径，但正在运行的正式版仍会在
# 收展/退出时向真实 config.json 落盘。若两者并行，运行器会把正式程序的自然保存
# 误判成测试污染，更严重的是用户窗口高度可能被改写。测试前只做检测并拒绝启动，
# 不自动关闭用户正在看的行情窗口。
# v1.4.55 改名：新版在 /Applications/Skyrocket.app，≤v1.4.54 的旧版还在旧路径时
# 同样算「正式版在运行」—— 两代都要认，漏一代就是拿用户的行情窗口做实验。
INSTALLED_APP_PATTERN="/Applications/Skyrocket.app/Contents/MacOS/Skyrocket|/Applications/实时股价-目标价版.app/Contents/MacOS/StockFloatTargetPrice"
if pgrep -f "$INSTALLED_APP_PATTERN" >/dev/null 2>&1 \
  || pgrep -x "Skyrocket" >/dev/null 2>&1 \
  || pgrep -x "StockFloatTargetPrice" >/dev/null 2>&1; then
  echo "!! 检测到正在运行的正式版（Skyrocket / 旧名 实时股价-目标价版）。" >&2
  echo "!! 请先退出后再运行回归，避免用户窗口配置被并行保存。" >&2
  exit 2
fi
# SF_GUARD_PROBE=1：只验上面那道守卫，验完就走。为什么要有这个开关 ——
# 守着它的用例（tests/test_run_tests_guard.py）是**真起一个 run_tests.sh 子进程**、
# 用假 pgrep 喂答案的：一旦假 pgrep 与脚本里的模式对不上，子进程就会把整轮回归
# 再跑一遍（实测 112 秒，还会嵌套跑挂）。有了短路，判错立刻返回，不烧一整轮。
if [ -n "${SF_GUARD_PROBE:-}" ]; then
  echo "== 守卫探针：未被拦下（正式版未在运行）=="
  exit 0
fi

# ---- 互斥：同一时间只许一轮完整回归（2026-10-21）----
# 为什么：两个会话同时起整档时，五个落盘路径在同一个 TMPDIR 下互相 rm -f，同一棵树的
# build/.coverage-provenance.json 又被后起一轮的 begin 覆盖成 invalid。当天 12:30~12:56
# 起了四轮，三轮的覆盖率结论被判"来源不可信"，每轮白烧约 9 分钟，谁都没拿到干净结论。
# 做法：两把符号链接锁。ln -s 是原子创建，目标里写「pid|进程启动时刻|工作目录」，
# 不存在"刚建出来还是空的"那一瞬。一把按临时目录算（保护共享的 sf_*_test 文件），
# 一把按本棵树的 build/ 算（保护覆盖率回执）。拿不到就退出码 75，不排队、不抢、不删别人的锁。
# 只有持有者已不在、或 pid 被别的进程复用（启动时刻对不上）才算陈旧；启动时刻读不到时
# 退回只看进程是否还在（读不到不等于已陈旧）；回收时再抢一个
# 互斥目录，免得两个人同时回收时把对方刚建好的活锁当成陈旧的拆掉。
SF_LOCK_TMP_PATH=""
SF_LOCK_TREE_PATH=""
SF_LOCK_RECORD=""
sf_lstart_of() {
  LC_ALL=C ps -p "$1" -o lstart= 2>/dev/null | awk '{$1=$1; print}'
}
sf_lock_owner_alive() {
  local pid="${1%%|*}" rest="${1#*|}" recorded now
  case "$pid" in ""|*[!0-9]*) return 1 ;; esac
  recorded="${rest%%|*}"
  now="$(sf_lstart_of "$pid")"
  # 两边的启动时刻都读得到：严格比对，对不上就是 pid 被别的进程复用，不是持有者。
  if [ -n "$recorded" ] && [ -n "$now" ]; then
    [ "$now" = "$recorded" ]
    return
  fi
  # 任何一边读不到（写锁时 ps 取不到、或现在取不到）就证明不了 pid 被复用。
  # 此时不能按「陈旧」回收一个可能还活着的持有者（那等于互斥悄悄失效、两轮并跑），
  # 退回只看这个进程是否还在：在就拒绝，不在才算陈旧。宁可误拦，不可放行。
  kill -0 "$pid" 2>/dev/null
}
sf_lock_refuse() {
  local who="${2%%|*}" rest="${2#*|}"
  echo "!! 已有另一轮完整回归占着${1}（进程 ${who}，启动于 ${rest%%|*}，目录 ${rest#*|}）。" >&2
  echo "!! 两轮同时跑会共用临时配置与覆盖率回执，谁都拿不到可信结论；请等它结束后再跑。" >&2
  echo "!! 只有确认那个进程已不存在、锁却还在时，才可手动处理锁文件；不要强杀别人的回归。" >&2
  exit 75
}
sf_lock_acquire() {
  local lock="$1" label="$2" record="$3" existing tries=0
  while [ "$tries" -lt 6 ]; do
    if ln -s "$record" "$lock" 2>/dev/null; then return 0; fi
    existing="$(readlink "$lock" 2>/dev/null || true)"
    if [ -n "$existing" ] && sf_lock_owner_alive "$existing"; then
      sf_lock_refuse "$label" "$existing"
    fi
    if [ -n "$existing" ] && mkdir "${lock}.reclaim" 2>/dev/null; then
      # 抢到回收权后再核一次：这条记录仍是刚才那条陈旧的，才拆。
      if [ "$(readlink "$lock" 2>/dev/null || true)" = "$existing" ]; then
        rm -f "$lock"
      fi
      rmdir "${lock}.reclaim" 2>/dev/null || true
    else
      sleep 0.2
    fi
    tries=$((tries + 1))
  done
  echo "!! 取不到互斥锁（${label}）：${lock} 的状态反复变化，或回收通道被占用。" >&2
  echo "!! 请确认没有别的回归在跑；${lock}.reclaim 若是遗留目录且无人在回收，可手动移走。" >&2
  exit 75
}
sf_lock_release() {
  # 只拆自己建的：目标记录仍是自己的才删，别人接手后的锁不动。
  local item
  for item in "$SF_LOCK_TMP_PATH" "$SF_LOCK_TREE_PATH"; do
    [ -n "$item" ] || continue
    if [ "$(readlink "$item" 2>/dev/null || true)" = "$SF_LOCK_RECORD" ]; then
      rm -f "$item"
    fi
  done
  return 0
}
SF_LOCK_ROOT="${TMPDIR:-/tmp}"
if [ -n "${SF_QUALITY_SESSION_DIR:-}" ] && [ -d "$SF_QUALITY_SESSION_DIR" ]; then
  SF_LOCK_ROOT="$SF_QUALITY_SESSION_DIR"
fi
SF_LOCK_TREE_ROOT="$(pwd)/build"
# SF_LOCK_PROBE=1：只验互斥，取到锁就走。锁目录只在探针模式下才可改道（SF_LOCK_PROBE_DIR），
# 否则一枚环境变量就能把两把锁挪走、等于关掉互斥；整轮真跑时这个变量不起任何作用。
if [ -n "${SF_LOCK_PROBE:-}" ] && [ -n "${SF_LOCK_PROBE_DIR:-}" ]; then
  SF_LOCK_ROOT="$SF_LOCK_PROBE_DIR"
  SF_LOCK_TREE_ROOT="$SF_LOCK_PROBE_DIR"
fi
mkdir -p "$SF_LOCK_ROOT" "$SF_LOCK_TREE_ROOT" || { echo "!! 无法创建互斥锁目录" >&2; exit 1; }
SF_LOCK_RECORD="$$|$(sf_lstart_of "$$")|$(pwd -P)"
trap sf_lock_release EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
SF_LOCK_TMP_PATH="${SF_LOCK_ROOT%/}/sf_run_tests.lock"
sf_lock_acquire "$SF_LOCK_TMP_PATH" "共享临时目录 ${SF_LOCK_ROOT}" "$SF_LOCK_RECORD"
SF_LOCK_TREE_PATH="${SF_LOCK_TREE_ROOT%/}/.run_tests.tree.lock"
sf_lock_acquire "$SF_LOCK_TREE_PATH" "本棵工作树的 build/" "$SF_LOCK_RECORD"
if [ -n "${SF_LOCK_PROBE:-}" ]; then
  echo "== 互斥探针：已取得两把锁 =="
  exit 0
fi

# 只接受质量执行器创建的会话边界；普通环境变量不构成删除配置文件的授权。
if [ -n "${SF_QUALITY_SESSION_DIR:-}" ]; then
  "$PY" -c '
import os
from pathlib import Path
root = Path(os.environ["SF_QUALITY_SESSION_DIR"]).resolve(strict=True)
expected = {"SF_CONFIG_PATH": "config.json", "SF_LAST_QUOTE_PATH": "quote.json",
            "SF_UNIVERSE_PATH": "universe.json", "SF_LOG_PATH": "debug.log",
            "SF_SINGLE_SOCK": "single.sock"}
for key, name in expected.items():
    value = Path(os.environ.get(key, ""))
    if value.is_symlink() or value.resolve() != root / name:
        raise SystemExit("不安全的质量会话路径: " + key)
' || exit 1
else
  unset SF_CONFIG_PATH SF_LAST_QUOTE_PATH SF_UNIVERSE_PATH SF_LOG_PATH SF_SINGLE_SOCK
fi

# v1.3.45：首屏数据快照（last_quote.json）重定向到临时目录。
# 不重定向的话，用例之间会互相"预填"上一条行情：A 用例写完、B 用例构造主窗时就
# 直接把 A 的价格填进首屏 —— 「无数据显示 --」这一整类断言会被打穿，
# 同时测试还会往用户真实的 ~/Library/Application Support/ 里写东西。
export SF_LAST_QUOTE_PATH="${SF_LAST_QUOTE_PATH:-${TMPDIR:-/tmp}/sf_last_quote_test.json}"
rm -f "$SF_LAST_QUOTE_PATH"
# v1.3.50：主配置 config.json 也必须重定向到临时目录。
# 外层 quality_evidence.run 若已提供会话目录，则必须沿用它，保证 receipt 与落盘现场同源。
export SF_CONFIG_PATH="${SF_CONFIG_PATH:-${TMPDIR:-/tmp}/sf_config_test.json}"
rm -f "$SF_CONFIG_PATH" "$SF_CONFIG_PATH.bak"
# v1.3.69：测试运行器本身也显式隔离所有落盘物；避免覆盖率/子进程
# 尚未加载 sf_constants 时先写入真实日志或单实例 socket。
export SF_UNIVERSE_PATH="${SF_UNIVERSE_PATH:-${TMPDIR:-/tmp}/sf_securities_test.json}"
export SF_LOG_PATH="${SF_LOG_PATH:-${TMPDIR:-/tmp}/sf_debug_test.log}"
export SF_SINGLE_SOCK="${SF_SINGLE_SOCK:-${TMPDIR:-/tmp}/sf_single_test.sock}"
# 同时关掉首屏预填：否则同一进程里 A 用例写完快照、B 用例构造主窗时就被预填，
# 「无数据显示 --」这一整类断言会互相串味。预填功能本身由 tests/test_v1345.py 单独验。
export SF_SEED_FIRST_SCREEN=0

# ---- 第 0 关：静态检查闸门（放在"正式版在跑就拒跑"之后）----
# 顺序不是随手排的：那条红线约定 exit 2（tests/test_run_tests_guard.py 守着），
# 而本关卡红是 exit 1 —— 排在它前面会把"请先退出正式版"这件事伪装成"代码有问题"。
# 本关卡只读源码、不碰用户数据，所以放在数据隔离之前也不污染任何东西。
# 守的是"写错了但运行时被 except 兜住、功能从此再没生效"这一整族缺陷
# （_objc_deps / bottom_rows / _chart_layer_key 都是这一族，各自潜伏了几十个版本）。
# 只读、耗时 0.2~2.5s（mypy 热/冷），所以每次提交都跑，不设跳过开关。
# 闸门自身不可信时结论也不算数：--self-test 会在种下的错误没被抓到时直接红。
echo "== 第 0 关：静态检查闸门（A 级硬零 + B 级棘轮）=="
"$PY" tools/public_docs_check.py || { echo "!! 公开文档缺失或不是本版内容" >&2; exit 1; }
"$PY" tools/static_gate.py || { echo "!! 静态检查闸门未过，后面的测试不必再跑" >&2; exit 1; }
"$PY" tools/static_gate.py --self-test --quiet >/dev/null || {
  echo "!! 静态闸门自检失败：检查器本身已不可信，本轮结论不算数" >&2; exit 1; }

# ---- 第 0.3 关：PyObjC 桥接层的形状核对（v1.4.57 起）----
# 起因是 iMac 实机一枚主线程 SIGSEGV（v1.4.55，点 Dock 图标即崩）：PyObjC 在建
# Python 子类那刻就要把方法的 ObjC 编码写进 runtime，取不到元数据时一律退化成
# "返回值与所有参数都是对象"，于是 hasVisibleWindows 这个 BOOL 被当成指针解引用。
# 同一份包在两台同版本 macOS 上一台正常一台必崩，差别只有"谁先 import"。
# 同一轮还扫出第二处：pyobjc 12 的 Foundation 已不导出 dispatch_async，sf_notify
# 那一整句 import 当场失败 → UN 通知路线静默降级 osascript，点击横幅恢复窗口没了。
# 这一族缺陷运行时全被 try/except 兜住、不崩就是不出现，ruff/mypy 也看不见，
# 所以单开一关：真实顺序登记的编码 vs 框架元数据的权威编码，不一致就红。
# 只读源码 + 起两个干净子进程，不碰窗口也不碰用户数据，耗时约 2s。
echo "== 第 0.3 关：PyObjC 签名与符号核对 =="
"$PY" tools/pyobjc_sig_check.py || {
  echo "!! PyObjC 桥接层核对未过（参数编码随 import 顺序变脸 / 符号取不到），详情见上" >&2
  exit 1; }
"$PY" tools/pyobjc_sig_check.py --self-test >/dev/null || {
  echo "!! PyObjC 核对闸门自检失败：种的错抓不到，检查器本身已不可信" >&2; exit 1; }


# ---- 第 0.5 关：底部两行的实渲染像素核验（改坏布局要秒级发现）----
# tools/verify_bottom_gap_px.py 早就存在，但一直只能靠人记得手动跑 —— 而"底部两行
# 重叠 / 掉到窗口外"恰恰是**只有真实渲染才看得到**的那类缺陷，本仓为此返工过两次
# （v1.2.63 判定式恒 -18px 时 w=200 回落率 100%、v1.3.53 基线对齐下沉 10px）。
# 它本身就是离屏脚本、单跑 0.4 秒，没有任何理由继续留在人工清单里。
# 放在数据隔离之后：它会构造真实 FloatWindow，必须吃到上面那几个 SF_* 重定向。
echo "== 第 0.5 关：底部两行实渲染像素核验 =="
GAP_LOG="/tmp/sf_bottom_gap_px_$$.log"
QT_QPA_PLATFORM=offscreen "$PY" tools/verify_bottom_gap_px.py >"$GAP_LOG" 2>&1 \
  || { echo "!! 底部两行像素核验未过（重叠 / 间距越界 / 画到界外），详情：" >&2
       tail -30 "$GAP_LOG" >&2; exit 1; }
grep "== 结论" "$GAP_LOG" || { echo "!! 像素核验没打印结论行，判定不可信" >&2; exit 1; }

# v1.3.68：改走 tools/test_runner.py —— 仍是标准 unittest，只是加了三道兜底：
#   ① 单用例超时（SF_TEST_TIMEOUT，默认 60s）：到点 dump 全线程栈并退出，
#      避免一个用例 hang 住让整轮测试无限等待；
#   ② 失败自动重跑并标记 [flaky]（SF_RERUN_FAILED=0 关闭）：把"抖了一次"
#      和"真的坏了"当场区分开，省掉每次十几分钟的人工判断；
#   ③ 随机执行顺序（SF_RANDOM_ORDER=1 开启）：发版前跑一次，暴露顺序依赖。
#      默认关闭 —— 随机序会让每次日志不可比，只用于主动排查。
# 另外运行器会在结尾校验用户数据（config / securities / debug.log）未被改动，
# 改了直接非零退出 —— 本仓有真实教训：一次回归把 bg 从 glass 写成 translucent，
# 症状伪装成"毛玻璃消失"，排障方向被带偏。
# 不加任何开关时，行为与原 `unittest discover` 完全一致。
# 覆盖率下限是**棘轮**（只许升不许降）：2026-09-24 补测轮把实测从 79% 抬到 87%，
# 下限随之从 76 抬到 84（留 3 个百分点余量给计时类用例的分支抖动）。
# 要把这个数再抬：先补用例，实测涨上来之后才改这里，不要先改数字再"凑"覆盖。
COVER_MIN="${SF_COVER_MIN:-}"
# 按模块的用例耗时明细：只是观测，不参与任何判定。默认写到本树 build/ 下，
# 方便事后回答"这一轮时间花在哪些模块"；显式设为空串可关掉，写失败只提示、不改结论。
export SF_TEST_DURATIONS_OUT="${SF_TEST_DURATIONS_OUT-$(pwd)/build/test-durations.json}"
echo "== 运行 tests/ 全部测试（tools/test_runner.py，覆盖率门禁 tools/coverage_gate.py）=="
# COVERAGE_GATE_RAN 决定末尾配不配落关卡时间戳：只有真跑过覆盖率门禁的那一轮才算全绿。
COVERAGE_GATE_RAN=0
# SF_LANES=N：把整套用例拆成 N 条车道并行跑，再合并成一个结论；不设则走原来的串行路径。
# 本脚本自己不设默认值（直接运行与云端回归仍是串行）；提交钩子在调用者没设置时替它定一个。
# 随机序仍走串行：它的目的是暴露顺序依赖，
# 和车道分配互相干扰。用法与取舍见 AGENTS.md 末尾“常用开关”。
# BEGIN lanes_switch（tests/test_lane_runner.py 把这一段抽出来在 /bin/sh 里逐个环境值验证）
SF_LANES_N="${SF_LANES:-}"
case "$SF_LANES_N" in ""|*[!0-9]*) SF_LANES_N="" ;; esac
[ "${SF_LANES_N:-0}" -gt 0 ] 2>/dev/null || SF_LANES_N=""
if [ "${SF_RANDOM_ORDER:-0}" = "1" ]; then SF_LANES_N=""; fi
# 质量执行器给的会话目录要求配置/日志/socket 路径全部落在它指定的位置，车道模式的
# 定向运行会把这些路径改到各自的临时目录，所以有会话目录时一律串行。
if [ -n "${SF_QUALITY_SESSION_DIR:-}" ]; then SF_LANES_N=""; fi
# END lanes_switch
if [ "${SF_SKIP_COVERAGE:-0}" = "1" ]; then
  if [ -n "$SF_LANES_N" ]; then
    "$PY" tools/lane_runner.py --lanes "$SF_LANES_N"
  else
    "$PY" tools/test_runner.py
  fi
elif [ "${SF_RANDOM_ORDER:-0}" = "1" ]; then
  # 随机序跑不计覆盖率：目的是暴露顺序依赖，覆盖率交给常规轮次
  SF_RANDOM_ORDER=1 "$PY" tools/test_runner.py
else
  "$PY" -m coverage erase
  "$PY" tools/coverage_provenance.py begin
  COVERAGE_RC=0
  if [ -n "$SF_LANES_N" ]; then
    "$PY" tools/lane_runner.py --lanes "$SF_LANES_N" --coverage || COVERAGE_RC=$?
  else
    "$PY" -m coverage run tools/test_runner.py || COVERAGE_RC=$?
  fi
  "$PY" tools/coverage_provenance.py finish --exit-code "$COVERAGE_RC" || exit 1
  "$PY" tools/coverage_provenance.py check || exit 1
  if [ "$PUBLIC_REGRESSION" = "1" ]; then
    echo "== 公开回归：无 privatebase，变更覆盖率标记 unverified-public-history =="
    echo "== 公开回归不是发布资格：仍将验证 global/modules，不落 gate_stamp =="
    "$PY" -c 'import json; from pathlib import Path; Path("build").mkdir(exist_ok=True); Path("build/change-coverage.json").write_text(json.dumps({"schema": 1, "status": "unverified-public-history", "ok": False, "reason": "公开源码没有 privatebase，无法证明变更范围覆盖率"}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")'
  else
    CHANGE_BASE="$("$PY" -c 'import os, subprocess, sys; from pathlib import Path; from tools.gate_stamp import git_env; anchor=Path("tools/change_coverage_base.txt").read_text().strip(); chosen=os.environ.get("SF_CHANGE_BASE") or anchor; resolve=lambda ref: subprocess.check_output(["git", "rev-parse", "--verify", ref+"^{commit}"], env=git_env(), text=True).strip(); anchor_id=resolve(anchor); chosen_id=resolve(chosen); check=subprocess.run(["git", "merge-base", "--is-ancestor", chosen_id, anchor_id], env=git_env(), check=False); check.returncode == 0 or sys.exit("SF_CHANGE_BASE cannot narrow the version-controlled comparison range"); print(chosen_id)')"
    "$PY" tools/change_coverage_gate.py --base "$CHANGE_BASE" --data-file "${COVERAGE_FILE:-.coverage}" --report-json build/change-coverage.json || exit 1
  fi
  # 分档断言在 tools/coverage_gate.py + tools/coverage_gate_baseline.json：
  # 产品档下限记在基线里（2026-10-13 起 89）；SF_COVER_MIN 只能临时**加严**，
  # 往基线以下压一律退 2 —— 一枚环境变量不该是降档通道（同下面那条"不落戳"的口径）。
  # 只许升；链上 tools/ 一档只降不升。
  # 为什么不再用一条全仓合并线：2026-10-10 实测全仓 83.63% 判红，而拆开是
  # 产品 88.27% / 链上 63.88% —— 一条线把"真缺口"和"子进程不记账"压成同一个数字，
  # 下一轮就会有人去给已经守住的代码重复补测，或者干脆把下限改小。
  if [ -n "$COVER_MIN" ]; then
    "$PY" tools/coverage_gate.py --min="$COVER_MIN"
  else
    "$PY" tools/coverage_gate.py
  fi
  if [ "$PUBLIC_REGRESSION" != "1" ]; then
    COVERAGE_GATE_RAN=1
  fi
fi

if [ "$PUBLIC_REGRESSION" = "1" ]; then
  echo "== 公开源码回归通过；差异覆盖未验证，不具备打包发布资格 =="
else
  echo "== 全部测试通过，可进行打包发布 =="
fi

# ---- 关卡时间戳（v1.4.59 起，S7-②）----
# 为什么放在最后一行之后：只有「整轮真的全绿」才配落戳，前面任何一段红都会 set -e 退出。
# 为什么必须有它：build_release.sh 此前从头到尾没调用过本脚本，而它做的每一件事都不可逆
# （覆盖桌面 DMG、替换 /Applications、把线上 OTA 清单指针切到这一版）。没有时间戳，
# 发版就没有任何东西能回答「这个包到底跑过回归没有」。
# 落的是内容指纹而不是 HEAD：未提交的改动同样进包，只提交文档则不该重跑两分钟回归。
# 写失败不改变本轮结论（测试确实全绿），但要大声说清楚 —— 下一轮发版会因此补跑回归，
# 静默降级会让人以为「关卡没跑」是关卡自己的问题。
#
# ⚠ 落戳的前提是**这一轮真的跑过覆盖率门禁**（v1.4.62 补的口子）。旧写法无条件落戳，
# 于是 SF_SKIP_COVERAGE=1 那一档跑完照样写"可用"—— 而 build_release.sh 第 0 步只看
# 指纹同源，同源就不补跑：一条环境变量就把覆盖率棘轮静默绕过去了。2026-10-10 实测踩中：
# 那一轮全仓合并值 83.63%（低于当时的 84 线），戳却是绿的。
# 现在这两档只跑用例、不落戳，发版侧会因此自动补跑一整轮完整回归 —— 这正是 S7-② 要的。
if [ "$COVERAGE_GATE_RAN" = "1" ]; then
  "$PY" tools/gate_stamp.py write \
    || echo "!! 关卡时间戳写入失败：发版脚本会因此强制补跑一轮回归（测试结论不受影响）" >&2
else
  echo "== 本轮没跑覆盖率门禁（SF_SKIP_COVERAGE / SF_RANDOM_ORDER），不落关卡时间戳 =="
  echo "   发版脚本会因此强制补跑一整轮完整回归；要跳过请显式用 --skip-tests，别靠环境变量。"
fi
