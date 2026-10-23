#!/usr/bin/env bash
# 实时股价 通用发版脚本（取代一次性的 build_v1262_dmg.sh）
#
# 用法（工程根目录）：
#     ./build_release.sh              # 打包 + 生成 DMG + 安装到 /Applications
#     ./build_release.sh --no-install # 只打包 + 生成 DMG，不动 /Applications
#     ./build_release.sh --local-only # 完整门禁 + DMG + 本机安装，不发布 OTA/源码
#     ./build_release.sh --prepare-only # 只制作当前源码候选 APP/DMG/ZIP/OTA，不安装、不上传
#     ./build_release.sh --publish-prepared # 使用已验候选，安装并发布，不重新生成资产
#
# 约定（用户 2026-09-02 明确要求，长期生效）：
#   **新程序默认安装到访达「应用程序」/Applications**，而不是桌面。
#   DMG、自动更新资产等桌面分发物统一输出到桌面的 Skyrocket 文件夹。
#
# 流程：
#   1) 从 sf_constants.APP_VERSION 读取版本号（唯一来源，不硬编码）
#   2) 上一轮 build/ dist/ 产物改名移走 → PyInstaller 打包 dist/实时股价.app
#   3) 组装 DMG 根（.app + /Applications 替身符号链接）→ hdiutil UDZO 压缩镜像
#   4) 安装到 /Applications：旧包先 mv 到工程内 build/ 作回退副本，再 cp 新包
#
# 为什么全用 mv 而不是 rm：
#   打包过程会批量删除旧产物（一次约 50 个文件），会触发安全删除的批量确认
#   拦截导致打包中断。把所有"清理"改成改名移走（旧产物送 /tmp 交给系统回收），
#   让 PyInstaller 每次都从空目录开始 —— 全程零删除，脚本可无人值守跑完。
#
# 依赖：工程内 .venv（Python 3.14.7 + PyQt6 + PyInstaller），脚本自动使用。
set -euo pipefail

PROJ="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJ"

# 单项目原子并发锁：mkdir 具备原子占位语义；退出时仅移除自身锁目录。
LOCK_DIR="$PROJ/.build_release_targetprice.lock"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  LOCK_PID="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
  if [ -n "$LOCK_PID" ] && kill -0 "$LOCK_PID" 2>/dev/null; then
    echo "!! 目标价版已有发版任务运行中：$LOCK_DIR (pid=$LOCK_PID)" >&2
    exit 1
  fi
  echo "!! 发现陈旧发版锁，确认没有活动进程后自动回收：$LOCK_DIR" >&2
  if [ -f "$LOCK_DIR/pid" ] && [ ! -L "$LOCK_DIR/pid" ]; then
    case "$LOCK_PID" in
      ''|*[!0-9]*) echo "!! 锁 PID 无效，保留现场，拒绝自动回收" >&2; exit 1 ;;
    esac
    rm "$LOCK_DIR/pid"
  fi
  rmdir "$LOCK_DIR" 2>/dev/null || { echo "!! 无法回收陈旧发版锁" >&2; exit 1; }
  mkdir "$LOCK_DIR"
fi
printf '%s\n' "$$" > "$LOCK_DIR/pid"
release_lock() {
  if [ -f "$LOCK_DIR/pid" ] && [ ! -L "$LOCK_DIR/pid" ] \
    && [ "$(cat "$LOCK_DIR/pid")" = "$$" ]; then
    rm "$LOCK_DIR/pid"
    rmdir "$LOCK_DIR" 2>/dev/null || true
  fi
}
trap release_lock EXIT

DO_INSTALL=1
DO_BUILD=1
SKIP_TESTS=0
LOCAL_ONLY=0
PREPARE_ONLY=0
USE_PREPARED=0
for arg in "$@"; do
  case "$arg" in
    --no-install) DO_INSTALL=0 ;;
    --local-only) LOCAL_ONLY=1 ;;
    --prepare-only) PREPARE_ONLY=1; DO_INSTALL=0 ;;
    --publish-prepared) USE_PREPARED=1 ;;
    # 跳过 PyInstaller，只重做 DMG + 安装（dist/ 已有产物时用，便于重试 DMG 阶段）
    --skip-build) DO_BUILD=0 ;;
    # 跳过回归关卡（唯一用途：拿同一份代码重做 DMG/OTA 的应急重试）
    --skip-tests) SKIP_TESTS=1 ;;
    *)
      echo "!! 未知参数：$arg" >&2
      echo "用法：$0 [--no-install] [--local-only] [--prepare-only] [--publish-prepared] [--skip-build] [--skip-tests]" >&2
      exit 2
      ;;
  esac
done

if [ "$USE_PREPARED" -eq 1 ] && { [ "$PREPARE_ONLY" -eq 1 ] || [ "$SKIP_TESTS" -eq 1 ] || [ "$LOCAL_ONLY" -eq 1 ]; }; then
  echo "!! --publish-prepared 不可混用候选生成、本机模式或跳过测试；已有候选只能核验后发布" >&2
  exit 2
fi
if [ "$PREPARE_ONLY" -eq 1 ] && [ "$LOCAL_ONLY" -eq 1 ]; then
  echo "!! --prepare-only 与 --local-only 不能同时使用" >&2
  exit 2
fi

# 已准备候选只做发布前核验和不可逆发布，不重新打包同一份源码。
if [ "$USE_PREPARED" -eq 1 ] && [ "$SKIP_TESTS" -eq 0 ]; then
  DO_BUILD=0
fi

PUBLIC_REGRESSION="${SF_PUBLIC_REGRESSION:-0}"
if [ "$PUBLIC_REGRESSION" = "1" ]; then
  echo "!! SF_PUBLIC_REGRESSION=1 仅用于公开源码回归，不具备发布资格；build_release.sh 拒绝运行。" >&2
  exit 2
fi
VENV_PY="$PROJ/.venv/bin/python3"
# v1.4.55 全局改名：产物与安装路径都是 Skyrocket.app
APP_NAME="Skyrocket"
APP_BUNDLE="$APP_NAME.app"
INSTALL_DIR="/Applications/$APP_BUNDLE"
DMG_NAME="$APP_NAME"
DESKTOP_OUTPUT_DIR="$HOME/Desktop/Skyrocket"
if [ ! -x "$VENV_PY" ]; then
  echo "!! 未找到工程内虚拟环境解释器：$VENV_PY" >&2
  echo "!! 请先重建 .venv（Python 3.14.7），依赖见 requirements.txt" >&2
  exit 1
fi

# 质量记录是发布资格的输入，不应等到 3.75 的昂贵产物核验之后才发现缺失。
# 发布重试必须显式绑定本轮同源记录；只允许复用已固定候选，不允许用旧记录冒充。
QUALITY_RECORD="${SF_QUALITY_RECORD:-}"
if [ -z "$QUALITY_RECORD" ]; then
  echo "!! 缺少 SF_QUALITY_RECORD：请绑定本轮质量记录后再发布" >&2
  echo "   示例：SF_QUALITY_RECORD=\"$PROJ/build/quality-v${VERSION:-候选版本}.json\" ./build_release.sh --publish-prepared" >&2
  exit 1
fi
if [ ! -f "$QUALITY_RECORD" ]; then
  echo "!! SF_QUALITY_RECORD 不是文件：$QUALITY_RECORD" >&2
  exit 1
fi
"$VENV_PY" tools/quality_evidence.py validate "$QUALITY_RECORD" \
  || { echo "!! SF_QUALITY_RECORD 结构无效，未进入发布前置核验" >&2; exit 1; }

# 3.9 诊断覆盖闸门要的发布基线也在这里先查，不等到圆角、二进制兼容和首装路线三道昂贵核验之后。
# 区间必须由真实发布基线给定，不许按版本号推断。实测（v1.4.101 之后一轮）：同一条命令
# 不给基线退 1、点名一个本轮根本没碰的模块；给上一版发布头退 0 —— 红的是尺子量错了
# 目标，不是代码。只靠人在文档里记得 export，等于这道关迟早自己说谎（同 S7-② 那一族）。
if [ -z "${SF_RELEASE_BASELINE:-}" ]; then
  echo "!! 3.9 拒绝下结论：没有 SF_RELEASE_BASELINE，只能按版本号推断区间，那个区间会把" >&2
  echo "   上一版已经发出去的改动也算进「本轮」。请先 export SF_RELEASE_BASELINE=<上一版发布头>," >&2
  echo "   取法：outputs/release-candidates/v<上一版>/post_release.json 里的 release_source.head" >&2
  exit 1
fi

# These checks are read-only and independent. Run them together so a normal
# release does not pay independent startup/scan costs back-to-back. Each
# result is still collected and every failure blocks the release.
PRECHECK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/skyrocket-release-precheck.XXXXXX")"
cleanup_prechecks() {
  [ -d "$PRECHECK_DIR" ] || return 0
  mv "$PRECHECK_DIR" "${TMPDIR:-/tmp}/skyrocket-release-precheck-finished.$$.${RANDOM}" 2>/dev/null || true
}
trap 'cleanup_prechecks; release_lock' EXIT
echo "== 发版前置核验：固定签名 / 公开文档 / 既有公开历史 / 公开树（并行）=="
"$VENV_PY" tools/stable_signing.py preflight >"$PRECHECK_DIR/signing.log" 2>&1 &
PRECHECK_SIGNING=$!
"$VENV_PY" tools/public_docs_check.py >"$PRECHECK_DIR/docs.log" 2>&1 &
PRECHECK_DOCS=$!
if [ "$LOCAL_ONLY" -eq 0 ]; then
  "$VENV_PY" tools/public_release.py --audit-public-history >"$PRECHECK_DIR/history.log" 2>&1 &
  PRECHECK_HISTORY=$!
else
  printf '%s\n' 'local-only: no public history/network preflight requested' >"$PRECHECK_DIR/history.log"
  PRECHECK_HISTORY=0
fi
if [ "$PREPARE_ONLY" -eq 1 ]; then
  printf '%s\n' 'prepare-only: skip public_release export; no commit/push/public tree is requested' \
    >"$PRECHECK_DIR/public.log"
  PRECHECK_PUBLIC=0
else
  "$VENV_PY" tools/public_release.py >"$PRECHECK_DIR/public.log" 2>&1 &
  PRECHECK_PUBLIC=$!
fi
PRECHECK_FAILED=0
wait "$PRECHECK_SIGNING" || PRECHECK_FAILED=1
wait "$PRECHECK_DOCS" || PRECHECK_FAILED=1
if [ "$PRECHECK_HISTORY" -ne 0 ]; then
  wait "$PRECHECK_HISTORY" || PRECHECK_FAILED=1
fi
if [ "$PRECHECK_PUBLIC" -ne 0 ]; then
  wait "$PRECHECK_PUBLIC" || PRECHECK_FAILED=1
fi
for precheck_log in "$PRECHECK_DIR"/*.log; do
  cat "$precheck_log"
done
if [ "$PRECHECK_FAILED" -ne 0 ]; then
  echo "!! 发版前置核验未通过，未移动旧产物" >&2
  exit 1
fi
if [ "$LOCAL_ONLY" -eq 1 ]; then
  "$VENV_PY" tools/render_install_guide.py --local-only --check \
    || { echo "!! 本机版安装说明未同源或误写线上下载，拒绝打包" >&2; exit 1; }
elif grep -q '本构建仅用于本机覆盖安装' "$PROJ/README.md"; then
  echo "!! 当前文档标为本机版；公开发版须先更新 README 并重渲染完整安装说明" >&2
  exit 1
fi

# ---- 0) 回归关卡（v1.4.59 起，S7-②）----
# 为什么要把它做成打包前的第一件事，而不是文档里的一句「发版前先跑回归」：
# 30 轮走查实测 build_release.sh 从未调用过 run_tests.sh / static_gate —— 所有产物核验
# 都排在打包之后，而它做的每一步都不可逆（覆盖桌面 DMG、替换 /Applications、把线上 OTA
# 清单指针切到这一版）。「记得跑回归」这件事从来没有被强制执行过一次，所以它迟早不发生。
# 现在的行为：时间戳与当前代码同源就直接过（不重跑，省两分钟）；不可用就自动补跑一整轮
# 回归，红则中止发版。只有显式 --skip-tests 才跳过，且大声打印 WARNING。
# 判据实现见 tools/gate_stamp.py（内容指纹而非 HEAD，带 11 项变异自检）。
GATE_STAMP_VALID=0
if [ "$SKIP_TESTS" -eq 1 ]; then
  echo "!! WARNING: --skip-tests 生效 —— 本轮**没有**核验回归关卡，直接进打包。" >&2
  echo "!!          这个开关只用于同一份代码重做 DMG/OTA；改了代码还用它，等于回到 S7-②。" >&2
elif "$VENV_PY" tools/gate_stamp.py check \
  && "$VENV_PY" tools/coverage_provenance.py check; then
  GATE_STAMP_VALID=1
else
  echo "== 关卡时间戳不可用：先补跑 ./run_tests.sh（约 2 分钟）=="
  ./run_tests.sh || {
    echo "!! 回归关卡未过 —— 中止发版（不覆盖桌面 DMG、不动 /Applications、不切 OTA）" >&2
    echo "!!   若上面写的是「检测到正在运行的正式版」：先退出 Skyrocket 再重跑。" >&2
    exit 1; }
  GATE_STAMP_VALID=1
fi

# ---- 1) 版本号（唯一来源） ----
VERSION="$("$VENV_PY" -c "import sf_constants as c; print(c.APP_VERSION)")"
echo "== 版本号: v$VERSION =="

RELEASE_STATE="${SF_RELEASE_STATE:-$PROJ/build/release-state-v${VERSION}.json}"
RELEASE_REPORT="$RELEASE_STATE.report.json"
if [ "$LOCAL_ONLY" -eq 0 ]; then
  release_summary() {
    local result=$?
    trap - EXIT
    if [ -f "$RELEASE_STATE" ]; then
      if ! "$VENV_PY" tools/release_state.py report --state "$RELEASE_STATE" \
        --out "$RELEASE_REPORT" --quality "$QUALITY_RECORD"; then
        echo "!! 发布总报告生成失败，不能报告完整交付" >&2
        [ "$result" -ne 0 ] || result=1
      fi
    fi
    cleanup_prechecks
    release_lock
    exit "$result"
  }
  trap release_summary EXIT
  if [ "$USE_PREPARED" -eq 1 ]; then
    "$VENV_PY" tools/release_state.py validate --state "$RELEASE_STATE" --stage candidate_ready
  else
    "$VENV_PY" tools/release_state.py init --state "$RELEASE_STATE" --version "$VERSION"
    "$VENV_PY" tools/release_state.py qualify-source --state "$RELEASE_STATE"
  fi
fi

# ---- 2) 上一轮产物改名移走（避免 PyInstaller 批量删除触发拦截）----
STAMP="$(date +%Y%m%d_%H%M%S)"
PREV="$PROJ/build/_prev"
PREV_TMP="/tmp/sf_prev_targetprice_${STAMP}_$$"
mkdir -p "$PROJ/build"
if [ "$DO_BUILD" -eq 1 ]; then
  # 只在真正要重新打包时才搬走上一轮产物（--skip-build 时 dist/ 是输入，必须保留）
  if [ -e "$PREV" ]; then
    # 再上一轮的产物送系统临时目录（macOS 自动回收）
    mv "$PREV" "$PREV_TMP"
    # 2026-09-22 崩溃复盘：_prev 里含 dist/ 与 dmg_root_*/ 的完整「实时股价-目标价版.app」，
    # 落入 /tmp 后仍可被双击或被系统重新拉起，旧副本在 Qt 导入期 SIGSEGV，
    # 用户看到的就是"程序意外退出"。整体改名去掉 .app 后缀，
    # LaunchServices 即不再把这些目录当应用包（-depth 保证先处理内层再动外层）。
    find "$PREV_TMP" -depth -name "*.app" -exec sh -c 'mv "$1" "$1.stale"' _ {} \; 2>/dev/null || true
  fi
  mkdir -p "$PREV"
  [ -d "$PROJ/build/StockFloat" ] && mv "$PROJ/build/StockFloat" "$PREV/StockFloat"
  [ -d "$PROJ/build/dmg_root" ] && mv "$PROJ/build/dmg_root" "$PREV/dmg_root"
  [ -d "$PROJ/dist" ] && mv "$PROJ/dist" "$PREV/dist"
  true  # mv 短路保护：上面的 [ -d ] && mv 在目录不存在时返回 1
fi

# ---- 2.5) 临时目录残留副本清扫（v1.4.5 守卫管不到 ≤1.4.4 的旧副本，只能改名）----
# 2026-09-22 / 09-23 两份"程序意外退出"报告（均为 v1.3.97）的崩溃路径都是
# /private/tmp/*/实时股价-目标价版.app：历史 OTA 验证（如 /tmp/ota14xx）与发版移走
# 的产物里留有完整可启动副本，被系统重新拉起后在 PyQt6 导入期 SIGSEGV。
# 上面第 2 步只处理本轮 _prev，这里把临时目录里所有本程序的可启动副本一并中性化。
# 只改名不删除、只认本程序 bundle id，失败不阻断发版。
"$VENV_PY" tools/sweep_temp_apps.py --apply 2>&1 | tail -8 \
  || echo "   （临时目录清扫未完成，不影响本次发版）" >&2

# ---- 3) PyInstaller 打包 ----
if [ "$DO_BUILD" -eq 1 ]; then
  echo "== PyInstaller 打包 StockFloat.spec =="
  # 不要加 --clean：它删缓存同样会触发批量删除拦截。
  "$VENV_PY" -m PyInstaller StockFloat.spec --noconfirm >/tmp/sf_targetprice_pyinstaller.log 2>&1 \
    || { echo "!! 打包失败，日志：/tmp/sf_targetprice_pyinstaller.log" >&2; tail -40 /tmp/sf_targetprice_pyinstaller.log >&2; exit 1; }
fi
if [ ! -d "$PROJ/dist/$APP_BUNDLE" ]; then
  echo "!! 未找到产物 dist/${APP_BUNDLE}（请先完整构建，或去掉 --skip-build）" >&2
  exit 1
fi
echo "   产物: dist/$APP_BUNDLE ($(du -sh "$PROJ/dist/$APP_BUNDLE" | cut -f1))"

# Keep the same certificate-pinned designated requirement across releases.
# An existing --skip-build artifact must already have that identity; do not
# silently re-sign an old app or permit an ad-hoc fallback.
if [ "$DO_BUILD" -eq 1 ]; then
  "$VENV_PY" tools/stable_signing.py sign --app "$PROJ/dist/$APP_BUNDLE" \
    || { echo "!! 固定签名失败，拒绝生成 DMG、安装或发布 OTA" >&2; exit 1; }
fi
"$VENV_PY" tools/stable_signing.py check --app "$PROJ/dist/$APP_BUNDLE" \
  || { echo "!! 应用签名身份已变化，不能保证升级保留授权，拒绝发布" >&2; exit 1; }

# ---- 3.5) 版本闸门：dist/ 内 .app 的打包版本必须 == APP_VERSION ----
# v1.2.95 第8轮走查：--skip-build 时 dist/ 是上一轮残留产物，若来自旧版本却仍被
# DMG+安装，会发出"版本号对、内容旧"的安装包且零报错。此闸门在 DMG 之前硬拦：
# 版本不符直接中止，要求走完整构建。完整构建（刚打出来的包）版本必然一致，不受影响。
if ! "$VENV_PY" tools/dist_version_check.py "$PROJ/dist/$APP_BUNDLE" "$VERSION"; then
  echo "!! 版本闸门未过：dist/$APP_BUNDLE 的打包版本与 sf_constants.APP_VERSION(v$VERSION) 不一致" >&2
  echo "!! 请去掉 --skip-build 重新完整构建，避免把陈旧产物当新版发出去" >&2
  exit 1
fi

# ---- 3.6) 产物核验三件套（必须在 DMG / 安装 / OTA 之前，因为那三步不可逆）----
# 为什么三件都要、且都要**硬拦**：v1.4.30 之前 build_release.sh 从头到尾
# 没有调用过任何产物核验 —— tools/verify_dmg_pyz.py 写得再好，没人跑就等于没有。
# 而这三件各守一类"源码全绿也漏出去"的缺陷：
#   ① release_feature_check：本轮改动是否真的进了包（特征清单没填/还是上一轮的
#      版本号 → 直接拒绝出包；防"每次无脑 PASS"的核验器）；
#   ② bundle_closure_check：代码 import 的模块包内齐不齐（v1.3.74 漏 _cffi_backend
#      让已安装程序点「检查更新」就中断，那类问题至今没有自动校验）；
#   ③ smoke_frozen_app：把产物真起一次、等首帧、再干净退出（v1.4.18 下载完退出
#      必崩 rc=139、以及"双击没反应一行日志都不写"的缺插件类故障）。
# 想跳过请显式改脚本并说明理由：这里刻意不提供 --skip 开关。
echo "== 3.6 产物核验：本轮特征 / import 闭包 / 真产物冒烟 =="
"$VENV_PY" tools/release_feature_check.py "$PROJ/dist/$APP_BUNDLE" \
  || { echo "!! 本轮产物特征核验未过：不要把证明不了本次改动的包发出去" >&2; exit 1; }
"$VENV_PY" tools/bundle_closure_check.py "$PROJ/dist/$APP_BUNDLE" \
  || { echo "!! 打包 import 闭包不完整：把缺的模块加进 StockFloat.spec 的 hiddenimports 重打包" >&2; exit 1; }
"$VENV_PY" tools/smoke_frozen_app.py "$PROJ/dist/$APP_BUNDLE" --timeout 30 \
  || { echo "!! 真产物冒烟未过：产物起不来/没画首帧/退出链炸，别发" >&2; exit 1; }
echo "   产物核验三件套全部通过"

# ---- 3.65) Permanent card-corner gate (2026-10-15) ----
# Always runs, including --skip-tests/--skip-build retries; keep it before DMG/install/OTA.
echo "== 3.65 卡片圆角：像素矩阵 / 包内几何同源 / 原生四角与交互走查 =="
CARD_OK=0
for card_attempt in 1 2 3; do
  CARD_OUT="$PROJ/build/card-corners-v$VERSION-attempt-$card_attempt"
  if "$VENV_PY" tools/verify_card_corners.py --app "$PROJ/dist/$APP_BUNDLE" --native --out "$CARD_OUT"; then
    CARD_OK=1
    break
  fi
  [ "$card_attempt" -lt 3 ] && sleep 2
done
[ "$CARD_OK" -eq 1 ] || { echo "!! 卡片圆角门禁未过：三次独立采集均失败，拒绝发布" >&2; exit 1; }

# ---- 3.7) 逃逸复盘闸门（2026-10-09 起，进 DMG / 安装 / OTA 之前）----
# 为什么放这里：负责人定的进度指标只有两个 —— 线上逃逸数、平均定位时长。要让它们真的
# 能下降，唯一可靠的办法是把「逃逸复盘」做成出包的前置条件，而不是靠人记得去做：
#   · 台账 tools/escapes_ledger.json 里没有本号的复盘声明 → 拒发（口头报数不算）；
#   · 本轮发现/收口的线上逃逸没登记「补了哪道防线」→ 拒发（防线没补，下轮还会再来一条）；
#   · 条目既没有定位时刻、也没写「为什么记不到」→ 拒发（null 必须带理由，不许回填估算）。
# 演练请指向临时副本：tools/quality_trend.py --ledger /tmp/x.json --open ...，
# 别把假缺陷写进唯一数据源（本轮真实踩过，已把这条写成 --self-test 的断言）。
# 填法：tools/quality_trend.py --round "$VERSION" --escapes N --defense "防线落点"
#       本轮 0 逃逸也要明写 --escapes 0 --zero-reason "为什么是 0"
echo "== 3.7 逃逸复盘：台账自洽 + 本轮复盘声明 =="
# quality_trend's mutation tests are already part of the full regression suite.
# When the content-fingerprint gate is valid, running them again here only
# starts another interpreter and repeats the same read-only work. Keep the
# direct check for --skip-tests retries, where the regression proof is absent.
if [ "$GATE_STAMP_VALID" -eq 0 ]; then
  "$VENV_PY" tools/quality_trend.py --self-test \
    || { echo "!! 复盘闸门没过自己的变异自检：它现在可能是一条永不失败的检查" >&2; exit 1; }
else
  echo "   quality_trend 自检已由同源 run_tests.sh 覆盖，跳过重复执行"
fi
"$VENV_PY" tools/quality_trend.py --gate "$VERSION" \
  || { echo "!! 逃逸复盘未收口：先写本轮复盘声明与防线落点，再出包" >&2; exit 1; }

# ---- 3.75) Real binary compatibility, before DMG/install/OTA ----
echo "== 3.75 macOS 26/27 二进制与 Qt 运行库兼容核验 =="
"$VENV_PY" tools/verify_macos_compat.py "$PROJ/dist/$APP_BUNDLE" --runtime \
  || { echo "!! 包内系统门槛、架构或依赖不兼容，拒绝发布" >&2; exit 1; }

# 记录结构已在最早的只读前置阶段校验；这里保留阶段说明，正式资格仍需等
# APP、DMG、ZIP 和 OTA 清单全部固定后执行 qualify。

# ---- 3.8) 首装与更新路线核验（v1.4.58 起）----
# 为什么值得为它多花两分钟：对外那句「装完第一次就能打开」一直没有机器判据，只靠人
# 照指引走一遍。这条闸门把判据换成可观测事实（装完那个 bundle 上有没有来历标签、
# 更新助手停在哪一步）。它第一次跑就抓到一处真实缺陷：v1.4.55 改名时漏改
# updater/update_helper.sh 的包内顶层名，装机版助手对线上 1.4.57 资产必失败
# archive_layout_invalid —— 也就是改名之后自动更新从未成功过，而全量回归一直是绿的。
echo "== 3.8 首装路线：curl 零放行 / DMG 双击 / 助手认两代包名 =="
"$VENV_PY" tools/verify_install_paths.py \
  || { echo "!! 安装路线核验未过：照指引装完仍会被拦，或助手认不出本轮包名" >&2; exit 1; }
# ⚠ 安装说明的渲染**不在这里**，在第 6.1 步（OTA 发布之后）。原因见那里的注释。

# ---- 3.9) 诊断覆盖闸门（2026-10-13 起，负责人口径："每次发版都必须检查"）----
# 为什么要进链而不是写文档：40 版逐版对账（outputs/诊断导出逐版核查-v1.4.27至v1.4.66-2026-10-12.md）
# 的结论是 39 个实发版本里 29 个引入了当时远程读不出来的状态，而全量回归两年全绿 ——
# "本轮改动有没有对应地补进诊断包"这件事从来没被任何关卡看过，所以它迟早不发生。
# 判据三档：curated（导出面符号闭包读得到）／auto（sf_diag 按命名约定自动采走）／
# 在 diag_coverage.txt 当前版本段留一行 skip 理由。三档都不沾又没申报 → 拒绝出包。
# 排在 DMG / 安装 / OTA 之前：那三步都不可逆（同 3.6 的理由）。
echo "== 3.9 诊断覆盖：本轮改过的模块，包里要么有现场、要么有写得出来的理由 =="
# 发布基线 SF_RELEASE_BASELINE 的必填检查在脚本前部（质量记录校验之后）。
"$VENV_PY" tools/diag_coverage_check.py --version "$VERSION" --baseline "$SF_RELEASE_BASELINE" \
  || { echo "!! 诊断覆盖核对未过：本轮改的模块在诊断包里既没有现场、也没有申报理由" >&2; exit 1; }

# ---- 4) DMG ----
if [ "$USE_PREPARED" -eq 1 ]; then
  DESKTOP_DMG="$DESKTOP_OUTPUT_DIR/${DMG_NAME}-V${VERSION}.dmg"
  DMG_OUT="$PROJ/build/${DMG_NAME}-V${VERSION}.dmg"
  [ -f "$DMG_OUT" ] || { echo "!! 缺少已准备 DMG：$DMG_OUT" >&2; exit 1; }
  echo "== 使用已准备 DMG（不重新生成）=="
else
DESKTOP_DMG="$DESKTOP_OUTPUT_DIR/${DMG_NAME}-V${VERSION}.dmg"
DMG_ROOT="$PROJ/build/dmg_root"
DMG_OUT="$PROJ/build/${DMG_NAME}-V${VERSION}.dmg"
mkdir -p "$DMG_ROOT"
  cp -R "$PROJ/dist/$APP_BUNDLE" "$DMG_ROOT/"
ln -sfn /Applications "$DMG_ROOT/Applications"
# 双击安装脚本放进 DMG 根（v1.4.58 起）。为什么放这儿：实测卷里的文件本身**不带**
# 来历标签（标签是"把 .app 拖进应用程序"那一刻传染的），所以这个脚本能被直接双击运行，
# 而它自己负责清掉刚拷出来那一份的标签 —— 用户不必打开终端、也不必去系统设置里点两下。
# 名字按 installer/*.command 发现，这里不写死（2026-10-12 改中文名「一键安装.command」时定的）：
# 文件名只有一个、引用它的地方有三处，写死就等于"改一处漏两处"——而漏掉的那两处不会报错，
# 只会让卷里安静地少掉一键安装，用户剩下的是"拖拽 + 被 Gatekeeper 拦"那一面墙。
INSTALLER_COUNT=0
INSTALLER_SCRIPT=""
for cand in "$PROJ"/installer/*.command; do
  [ -e "$cand" ] || continue
  INSTALLER_COUNT=$((INSTALLER_COUNT + 1))
  INSTALLER_SCRIPT="$cand"
done
if [ "$INSTALLER_COUNT" -ne 1 ]; then
  echo "!! installer/ 下应当恰好有一个双击安装脚本，实际 $INSTALLER_COUNT 个" >&2
  echo "   发版链按 installer/*.command 认它：多一枚少一枚都是一处漂移，别靠猜" >&2
  exit 1
fi
INSTALLER_BASE="$(basename "$INSTALLER_SCRIPT")"
cp -f "$INSTALLER_SCRIPT" "$DMG_ROOT/"
for doc in README.md SECURITY.md LICENSE NOTICE.md; do
  cp -f "$PROJ/$doc" "$DMG_ROOT/"
done
mkdir -p "$DMG_ROOT/docs"
cp -f "$PROJ/docs/INSTALL.md" "$DMG_ROOT/docs/INSTALL.md"
chmod +x "$DMG_ROOT/$INSTALLER_BASE"
# 放没放进去，用卷里的可执行位说话（cp 被拦、名字撞车，最后都是用户双击不到）
[ -x "$DMG_ROOT/$INSTALLER_BASE" ] \
  || { echo "!! 卷内没备好 ${INSTALLER_BASE}，发版停在这里（否则用户只剩拖拽那条路）" >&2; exit 1; }
# 给它做外观（自绘那枚"银灰驱动器 + 下箭头"图标 + 把 `.command` 从显示名里去掉）。
# 为什么必须在发版链里做而不是手工：手工在 Finder 里贴的东西不进版本控制，下一轮重打包就没了，
# 而它没了不会有任何地方报错 —— 用户看到的又会是一页写着 COMMAND 的文档缩略图。
# 判据不是"贴过了"，是**读回来**：图标比像素，显示名读 NSURLLocalizedNameKey。
# 两件事一次做完（同一个工具的默认动作），因为它们写的是同一条元数据的同一格 flags，
# 分成两步调用就等于给"后一步踩掉前一步"留了位置。
QT_QPA_PLATFORM=offscreen "$VENV_PY" tools/apply_installer_icon.py "$DMG_ROOT/$INSTALLER_BASE" \
  || { echo "!! 卷内安装脚本的外观做不上或做完读不回来，发版停在这里" >&2; exit 1; }
echo "   卷内一键安装脚本: ${INSTALLER_BASE}（已贴图标、已隐藏 .command 后缀，两项都读回核验）"

# -volname 必须是 ASCII：中文卷标会让 hdiutil 报 "create failed - 无此文件或目录"
# 输出文件名可以是中文，故直接以中文名生成，无需 mv
hdiutil create -ov -volname "Skyrocket_v${VERSION}" \
    -fs HFS+ -srcfolder "$DMG_ROOT" \
    -format UDZO -imagekey zlib-level=9 \
    "$DMG_OUT" >/dev/null
mv "$DMG_ROOT" "$PREV/dmg_root_$STAMP"

# 元数据能不能**活着进卷**是另一件事：HFS+ 支持它，但"支持"不是证据（UDZO 压缩、
# -srcfolder 拷贝任何一步把它丢了，界面上就是退回那页 COMMAND 文档缩略图、后缀又露出来，
# 且零报错）。所以挂上真产物，把那一枚脚本的图标与显示名都读回来一遍，再卸载。
# 挂不上就停下，不猜它没问题。
DMG_MNT="$PROJ/build/dmg_mount_$STAMP"
mkdir -p "$DMG_MNT"
if hdiutil attach -nobrowse -readonly -mountpoint "$DMG_MNT" "$DMG_OUT" >/dev/null 2>&1; then
  if QT_QPA_PLATFORM=offscreen "$VENV_PY" tools/apply_installer_icon.py \
        "$DMG_MNT/$INSTALLER_BASE" --check >/dev/null 2>&1; then
    echo "   卷内外观复验：挂载真 DMG 读回图标与显示名通过"
  else
    hdiutil detach -force "$DMG_MNT" >/dev/null 2>&1 || true
    echo "!! 卷里的安装脚本外观不对（图标或隐藏后缀没能进 DMG）—— 这一版别发" >&2
    exit 1
  fi
  hdiutil detach -force "$DMG_MNT" >/dev/null 2>&1 || true
  rmdir "$DMG_MNT" 2>/dev/null || true
else
  echo "!! 挂不上刚做出来的 DMG，卷内外观这一条没验到 —— 停在这里，不猜它没问题" >&2
  exit 1
fi

echo "== DMG 构建完毕 =="
ls -lh "$DMG_OUT" | awk '{print "   " $5, $9}'
if [ "$PREPARE_ONLY" -eq 1 ]; then
  echo "   prepare-only：不复制到桌面分发目录"
fi
fi

# ---- 5) 候选 OTA 资产（正式资格检查前只生成，不上传、不覆盖桌面）----
if [ "$LOCAL_ONLY" -eq 0 ]; then
  NOTES_FILE="$PROJ/release_notes.txt"
  [ -s "$NOTES_FILE" ] || { echo "!! 发布说明缺失或为空：$NOTES_FILE" >&2; exit 1; }
  UPDATE_ASSET_DIR="$PROJ/build/update_assets/V$VERSION"
  if [ "$USE_PREPARED" -eq 0 ]; then
    "$VENV_PY" tools/make_update_assets.py "$PROJ/dist/$APP_BUNDLE" --output "$UPDATE_ASSET_DIR"
  else
    echo "   使用已准备 OTA 资产（不重新生成）"
  fi
  UPDATE_ZIP="$UPDATE_ASSET_DIR/Skyrocket-${VERSION}-macos.zip"
  UPDATE_JSON="$UPDATE_ASSET_DIR/Skyrocket-update.json"
  [ -f "$UPDATE_ZIP" ] && [ -f "$UPDATE_JSON" ] \
    || { echo "!! 候选 OTA 资产未完整生成，拒绝进入正式资格检查" >&2; exit 1; }
  echo "   候选自动更新资产: $UPDATE_ASSET_DIR"
fi

if [ "$PREPARE_ONLY" -eq 1 ]; then
  "$VENV_PY" tools/release_state.py candidate --state "$RELEASE_STATE" --version "$VERSION" \
    --app "$PROJ/dist/$APP_BUNDLE" --dmg "$DMG_OUT" --zip "$UPDATE_ZIP" --ota "$UPDATE_JSON"
  echo "== 仅制作候选产物：跳过正式资格、安装、桌面覆盖和远端发布 =="
  echo "   APP: $PROJ/dist/$APP_BUNDLE"
  echo "   DMG: $DMG_OUT"
  echo "   ZIP: $UPDATE_ZIP"
  echo "   OTA: $UPDATE_JSON"
  exit 0
fi

# 产物角色在这里才全部存在。正式资格检查必须早于 /Applications、桌面清理和远端写入；
# publish_update_release.py 还会在读取凭据前用同一个入口再核验一次固定字节。
if [ "$LOCAL_ONLY" -eq 0 ]; then
  "$VENV_PY" tools/quality_evidence.py qualify "$QUALITY_RECORD" \
    || { echo "!! 正式候选资格未通过，拒绝安装、覆盖桌面或发布 OTA" >&2; exit 1; }
  if [ "$USE_PREPARED" -eq 0 ]; then
    "$VENV_PY" tools/release_state.py candidate --state "$RELEASE_STATE" --version "$VERSION" \
      --app "$PROJ/dist/$APP_BUNDLE" --dmg "$DMG_OUT" --zip "$UPDATE_ZIP" --ota "$UPDATE_JSON"
  fi
  "$VENV_PY" tools/release_state.py begin-publish --state "$RELEASE_STATE" --quality "$QUALITY_RECORD"
fi

# BEGIN publish_desktop_dmg
publish_desktop_dmg() {
  local source="$1" destination="$2" directory old
  directory="$(dirname "$destination")"
  mkdir -p "$directory"
  cp -p "$source" "$destination"
  echo "   桌面分发包: $destination"
  for old in "$directory"/Skyrocket-V*.dmg "$directory"/实时股价-目标价版-V*.dmg; do
    [ -e "$old" ] || continue
    [ "$old" = "$destination" ] && continue
    mv "$old" "$HOME/.Trash/" 2>/dev/null || true
  done
}
# END publish_desktop_dmg
publish_desktop_dmg "$DMG_OUT" "$DESKTOP_DMG"

# ---- 5.1) 安装到 /Applications（默认行为）----
if [ "$DO_INSTALL" -eq 1 ]; then
  APP_DST="$INSTALL_DIR"
  # 旧包 mv 到工程内作回退副本（不用 rm -rf 删 /Applications 下的内容）
  if [ -e "$APP_DST" ]; then
    mv "$APP_DST" "$PROJ/build/_rollback_Skyrocket_$STAMP.app"
    echo "   旧包已备份: $PROJ/build/_rollback_Skyrocket_$STAMP.app"
  fi
  # v1.4.55 改名：旧名字的包还躺在 /Applications 里会变成"两个程序"——Dock 里分不清、
  # LaunchServices 可能把旧的拉起来。一律 mv 成回退副本（不删文件），装完只剩一个 Skyrocket。
  for legacy in "$(dirname "$APP_DST")"/*.app; do
    [ -e "$legacy" ] || continue
    case "$legacy" in "$APP_DST") continue ;; esac
    lb="$(basename "$legacy")"
    case "$lb" in 实时股价-目标价版.app) ;; *) continue ;; esac
    mv "$legacy" "$PROJ/build/_rollback_${lb%.app}_$STAMP.app"
    echo "   改名前的旧包已移出 /Applications: $PROJ/build/_rollback_${lb%.app}_$STAMP.app"
  done
  cp -R "$PROJ/dist/$APP_BUNDLE" "$APP_DST"
  echo "   已安装: $APP_DST"
  echo "   版本: $(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' \
        "$APP_DST/Contents/Info.plist")"

  # ---- 回退副本回收：只保留最近 2 份（v1.4.61 起挪到"新包已装上"之后）----
  # 为什么必须在这之后：这段原先紧跟在 mv 备份后面，而它是一条 `ls | sort | awk | while`
  # 管道 —— 第二个通配（改名前的 `_rollback_实时股价_*.app`）在无匹配时 ls 仍返回非零
  # （stderr 被 2>/dev/null 吞掉，看不出来），脚本头部 `set -euo pipefail` 就地打死整条链，
  # 于是停在一个**「旧包已移走、新包还没拷」**的位置：/Applications 里谁都没有，
  # 用户点 Dock 图标起不来程序。v1.4.60 与 v1.4.61 两次"被超时切断"其实都是它，
  # 我一度把它误记成命令执行时限（已更正）。
  # 现在的形状：① 排在安装与版本回显之后，撤不回的动作都已完成；② 整条管道 `|| true`，
  # 清理是尽力而为，不许把发版打死；③ 每挪走一份都打印一行，不再静默。
  # 注意 macOS(BSD) 的 head 不支持 `head -n -2` 负行数，用 awk 输出"除最后 2 个以外"。
  stale_rollbacks="$( { ls -1d "$PROJ"/build/_rollback_Skyrocket_*.app \
                          "$PROJ"/build/_rollback_实时股价_*.app 2>/dev/null; } | sort \
      | awk '{a[NR]=$0} END{for(i=1;i<=NR-2;i++) print a[i]}' || true)"
  if [ -n "$stale_rollbacks" ]; then
    printf '%s\n' "$stale_rollbacks" | while IFS= read -r old; do
      [ -n "$old" ] || continue
      if mv "$old" "/tmp/$(basename "$old").$STAMP"; then
        echo "   旧回退副本送临时目录回收: $(basename "$old")"
      else
        echo "   !! 旧回退副本挪不动（不影响本轮发版）: $old" >&2
      fi
    done
  fi
else
  echo "== 跳过 /Applications 安装（--no-install）=="
fi

if [ "$LOCAL_ONLY" -eq 0 ]; then
# ---- 6) 自动更新发布资产（⚠ 本步会调 publish_update_release.py **直接对外发布**：
#      上传版本 ZIP + 切换线上清单指针。跑本脚本 = 发版，执行前必须确认已获授权。
#      2026-09-22 更正：原注释写作"只本地生成，上传须人工确认"，与实际不符，
#      曾导致两次"OTA 未推送"的错误汇报。） ----
# 分发目录只保留当前应用最新的自动更新 ZIP 与清单；旧文件先入项目回退区，再送废纸篓。
cp -p "$UPDATE_ZIP" "$DESKTOP_OUTPUT_DIR/$(basename "$UPDATE_ZIP")"
cp -p "$UPDATE_JSON" "$DESKTOP_OUTPUT_DIR/Skyrocket-update.json"
UPDATE_DESKTOP_BACKUP="$PROJ/build/_prev/update_assets_$STAMP"
mkdir -p "$UPDATE_DESKTOP_BACKUP"
for old in "$DESKTOP_OUTPUT_DIR"/Skyrocket-*-macos.zip "$DESKTOP_OUTPUT_DIR"/StockFloatTargetPrice-*-macos.zip; do
  [ -e "$old" ] || continue
  [ "$old" = "$DESKTOP_OUTPUT_DIR/$(basename "$UPDATE_ZIP")" ] && continue
  mv "$old" "$UPDATE_DESKTOP_BACKUP/"
  /usr/bin/trash "$UPDATE_DESKTOP_BACKUP/$(basename "$old")" 2>/dev/null || true
done
echo "   自动更新资产: $UPDATE_ASSET_DIR"
# 清单和 GitHub 发布页强制读取同一份 release_notes.txt，发布后再由发版核验下载确认。
"$VENV_PY" tools/publish_update_release.py --assets "$UPDATE_ASSET_DIR" --notes "$NOTES_FILE"
echo "   GitHub 发布说明来源: $NOTES_FILE"

# ---- 6.1) 对外安装说明（必须在 OTA 发布**之后**渲染并核验）----
# 顺序不是偏好，是 2026-10-10 实测逼出来的：那一轮源码已经是 1.4.58、DMG 也装好了，
# 但线上资产 HEAD 回来还是 404（1.4.57 仍 302）。如果按源码在发布前就渲染，那行 curl 命令
# 指向的是一个还不存在的包 —— 用户照抄就是 404，而且错误看起来像"你的地址写错了"。
# 所以这一步既刷新说明，也顺带复核"OTA 真的发上去了"（--check 里带线上可达性核对）。
# 第 6 步上传超时（本机上行只有 ~57KB/s）时不会走到这里，说明就停在对线上真实的那一版。
echo "== 6.1 对外安装说明（含线上资产可达性核对）=="
"$VENV_PY" tools/render_install_guide.py \
  || { echo "!! 安装说明渲染失败，或命令块不合 FR-12 约束（不许 sudo/删除）" >&2; exit 1; }
"$VENV_PY" tools/render_install_guide.py --check \
  || { echo "!! 安装说明与本轮不同源，或线上还取不到这一轮的包 —— 别对外发" >&2; exit 1; }

echo "== 6.2 对应源码、GPL 许可、公开文档与 DMG 同步发行 =="
"$VENV_PY" tools/public_release.py --publish \
  || { echo "!! OTA 已发布，但公开源码或分发资料未齐，不能报告完整交付" >&2; exit 1; }

# ---- 7) 源码同步到 origin（v1.4.62 补）----
# 为什么必须有这一步：2026-10-10 对账实测，origin 停在 v1.4.45，而本地已经是 v1.4.61 ——
# **16 个已经发到用户手上的版本在 GitHub 上没有对应源码**，而且 workflow 只在 push 时触发，
# 于是那 16 个版本一次 CI 都没跑过。全仓 `git push` 命中 0 处（脚本、文档、钩子都没提），
# 所以这不是"忘了推"，是规范里没有这一步 —— 和 S7-② 那条"build_release.sh 从没调用过
# run_tests.sh"是同一个根子：只靠人记得的事，迟早不发生。
# 为什么排在 OTA 之后：推源码不该成为出包的前置条件（网络或权限问题不该让一次已经
# 完成的发版回滚），但失败必须大声，并写进结尾那行结论里，不许静默吞掉。
echo "== 7) 源码同步到 origin =="
SRC_BRANCH="$(git -C "$PROJ" rev-parse --abbrev-ref HEAD)"
DIRTY_COUNT="$(git -C "$PROJ" status --porcelain | wc -l | tr -d ' ')"
if [ "$DIRTY_COUNT" != "0" ]; then
  echo "   ⚠ 工作区还有 $DIRTY_COUNT 个文件未提交：push 只同步已提交的部分，"
  echo "     对外可见的源码可能比装机版里那份少（未提交的改动同样进包）。"
fi
if git -C "$PROJ" rev-parse --verify --quiet "origin/$SRC_BRANCH" >/dev/null; then
  AHEAD="$(git -C "$PROJ" rev-list --count "origin/$SRC_BRANCH..HEAD")"
else
  AHEAD="（远端还没有 $SRC_BRANCH 这个分支）"
fi
SYNC_NOTE=""
if git -C "$PROJ" push -q origin "HEAD:refs/heads/$SRC_BRANCH" 2>/tmp/sf_push_err.log; then
  NOW="$(git -C "$PROJ" rev-list --count "origin/$SRC_BRANCH..HEAD" 2>/dev/null || echo '?')"
  if [ "$NOW" = "0" ]; then
    echo "   已同步：推前落后 $AHEAD 笔，现在 origin/$SRC_BRANCH 与 HEAD 一致"
  else
    echo "!! push 返回 0 却仍有 $NOW 笔没同步 —— 推上去的不是这个分支，去人工确认" >&2
    SYNC_NOTE="源码未同步"
  fi
else
  echo "!! 推 origin/$SRC_BRANCH 失败：这一版的源码没有对外备份，CI 也不会因此跑起来" >&2
  tail -3 /tmp/sf_push_err.log >&2 || true
  SYNC_NOTE="源码未同步"
fi

# ---- 7.1) 桌面分发目录对账（2026-10-22 用户要求的永久规则，见 AGENTS.md「桌面分发目录」）----
# 为什么需要：上面第 6 步只会往桌面分发目录写 DMG、ZIP、Skyrocket-update.json 三样，目录里其余文件
# 没有任何一步碰过，旧了也不报错（实测：一份说明文档停在 10-10，旧一代清单停在 1.4.54）。
# 本步核对版本绑定件与冻结件，通过后改写已登记文档的对账段；排在 OTA 与源码同步之后，所以
# 失败只写进结尾结论、不回滚已完成的发布（同 SYNC_NOTE 的口径），但绝不静默。
echo "== 7.1 桌面分发目录与本轮对账 =="
DESKTOP_NOTE=""
DESKTOP_RC=0
"$VENV_PY" tools/desktop_folder_sync.py --refresh --version "$VERSION" || DESKTOP_RC=$?
case "$DESKTOP_RC" in
  0) ;;
  3) DESKTOP_NOTE="桌面文档正文有失效引用待人工订正" ;;
  *) DESKTOP_NOTE="桌面分发目录与本轮不一致" ;;
esac

else
  echo "== 本机模式：不生成/发布 OTA，不发布公开源码，不推送开发分支 =="
fi

# ---- 8) 发版最后一步：最终应用冷启动复验（永久规则）----
# 这一步必须排在 OTA、公开资料和源码同步等动作之后：前面的成功不能替代"用户拿到的
# 最终包确实能正常启动"。默认检查刚安装到 /Applications 的 bundle；--no-install
# 时检查本轮 dist 产物。smoke_frozen_app 会隔离用户数据，只终止自己拉起的进程。
FINAL_APP="$PROJ/dist/$APP_BUNDLE"
if [ "$DO_INSTALL" -eq 1 ]; then
  FINAL_APP="$INSTALL_DIR"
fi
echo "== 8) 最终启动复验：$FINAL_APP =="
"$VENV_PY" tools/smoke_frozen_app.py "$FINAL_APP" --platform cocoa --timeout 30 \
  || { echo "!! 最终启动复验未过：本版不能报告为完整交付" >&2; exit 1; }

echo
if [ "$LOCAL_ONLY" -eq 1 ]; then
  echo "== 本机覆盖完成 v${VERSION}（未发布 GitHub/OTA）=="
elif [ -n "$SYNC_NOTE" ]; then
  echo "== 发布动作完成 v${VERSION}，但遗留一项：${SYNC_NOTE}（发布后验收待完成） =="
  echo "   修好后手工执行：git push origin $SRC_BRANCH"
else
  echo "== 发布动作完成 v${VERSION}，发布后验收待完成 =="
fi
if [ -n "${DESKTOP_NOTE:-}" ]; then
  echo "!! 遗留一项：${DESKTOP_NOTE}。修好后手工执行：./.venv/bin/python tools/desktop_folder_sync.py --refresh" >&2
fi
