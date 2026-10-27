#!/bin/sh
# 实时股价-目标价版：跟手性实机复验（双击运行，面向 iMac 等第二台机器）
#
# 为什么需要它：v1.4.28/v1.4.29 的跟手性结论全部来自 MacBook Air 本机 + 合成事件。
#   合成事件跳过了 macOS 对指针事件的合并，所以那些数字是**偏高的上界**；
#   而 v1.4.29 的实验开关 system_drag（把窗口位移交给 macOS 窗口服务器）
#   在合成事件下永远返回"不支持"，**只有真触控板才能验证**毛玻璃背景会不会掉队。
#   本脚本把"能自动查的"查掉，把"只能人感觉的"变成三问，并把取证文件收齐。
#
# 安全边界（全程只读用户数据）：
#   · 不碰 config.json / securities.json，不写「应用程序」目录内部；
#   · 只**读**运行日志，把摘要写到桌面一个文件里；
#   · 开关 system_drag 用 launchctl 环境变量，不改包内文件、不影响签名，
#     随时可用 --off 还原。

# ⚠ 身份必须**两代都认**（2026-10-12 全方位复核 F2）：v1.4.55 起程序叫
#   Skyrocket / com.skyrocket.app，而本工具当时只写旧名 —— 在装机版机器上
#   「未在应用程序找到本程序」直接 exit 1，iMac 实机复验整条路线跑不起来。
#   名单与 sf_constants 是否同步，由 tests/test_standalone_command_tools.py 钉住。
BUNDLE_IDS="com.skyrocket.app com.stockfloat.targetprice"
APP_NAMES="Skyrocket.app 实时股价-目标价版.app"
APP_DIR="$HOME/Library/Application Support/StockFloatTargetPrice"
LOG="$APP_DIR/debug.log"
DESKTOP_OUTPUT_DIR="$HOME/Desktop/Skyrocket"
mkdir -p "$DESKTOP_OUTPUT_DIR"
REPORT="$DESKTOP_OUTPUT_DIR/跟手性实机复验-$(date +%Y%m%d-%H%M%S).txt"
MODE="${1:-ask}"

# 先处理"关掉实验开关"：不依赖后面的任何检查，随时可还原
if [ "$MODE" = "--off" ]; then
  launchctl unsetenv STOCKFLOAT_FEATURES 2>/dev/null
  echo "已清除 STOCKFLOAT_FEATURES 环境变量（system_drag 回到默认关闭）。"
  echo "重启程序后生效（右键菜单退出 → 重新打开）。"
  exit 0
fi

line() { printf '%s\n' "----------------------------------------------"; }

echo "=============================================="
echo " 实时股价 / Skyrocket · 跟手性实机复验"
echo "=============================================="

# ---------- 1. 版本与运行状态 ----------
INSTALLED=""
for CAND in $APP_NAMES; do
  if [ -d "/Applications/$CAND" ]; then INSTALLED="/Applications/$CAND"; break; fi
done
PLIST="$INSTALLED/Contents/Info.plist"
if [ -n "$INSTALLED" ] && [ -f "$PLIST" ]; then
  VER="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$PLIST" 2>/dev/null)"
  echo "已安装版本：$INSTALLED (v$VER)"
else
  echo "未在「应用程序」找到本程序 —— 请先安装，再运行本工具。"
  echo "   （认的是这两代名字：${APP_NAMES}）"
  exit 1
fi
case "$VER" in
  1.4.2[0-8]|1.[0-3].*)
    echo "  ⚠ 复验结论需要 v1.4.29 或更新（抬层修复 + 打字链路 + system_drag 开关都在这一版）。"
    echo "    可在程序右键菜单里「检查更新」，或用桌面的 DMG 重装。"
    ;;
esac
# 两代包标识与两代可执行名一起找：改名后的进程叫 Skyrocket，改名前叫 StockFloatTargetPrice
PGREP_PATTERN=""
set -f    # 名单项按空格拆开，不许把其中的 * ? 展开成当前目录的文件名
for ITEM in $BUNDLE_IDS $APP_NAMES; do
  PGREP_PATTERN="${PGREP_PATTERN:+${PGREP_PATTERN}|}${ITEM}"
done
set +f
PID="$(pgrep -f "$PGREP_PATTERN" | head -1)"
[ -n "$PID" ] && echo "运行中：PID $PID" || echo "未在运行（下面的手感三问需要先启动程序）"

{
  echo "跟手性实机复验 · $(date '+%Y-%m-%d %H:%M:%S')"
  echo "机器：$(scutil --get ComputerName 2>/dev/null)  系统：$(sw_vers -productVersion 2>/dev/null)"
  echo "已安装版本：v$VER    运行：${PID:-否}"
} > "$REPORT"

# ---------- 2. 屏幕（决定"一帧多少毫秒"这把尺子） ----------
# 实测 system_profiler 在本机/常见 Mac 上不输出 Hz，只有分辨率：那就如实写"读不到"，
# 不要印一个"未知"让人以为工具坏了；Hz 请对照 系统设置 → 显示器。
RES="$(system_profiler SPDisplaysDataType 2>/dev/null | grep -m1 'Resolution' | sed 's/^ *//')"
echo "显示器：${RES:-未读到}"
echo "  提示：刷新率请对照 系统设置 → 显示器（60Hz ⇒ 一帧 16.7ms，是手感判据的尺子）"
echo "显示器：${RES:-未读到}（刷新率请人工对照系统设置）" >> "$REPORT"

# ---------- 3. 日志取证：置顶重排与异常 ----------
line
if [ -f "$LOG" ]; then
  echo "运行日志（最近 2000 行）里的关键信号："
  RETOP="$(grep -c 're-top' "$LOG" 2>/dev/null)"
  ERRN="$(grep -c ' ERROR' "$LOG" 2>/dev/null)"
  echo "  · 置顶重排（re-top）条数：${RETOP:-0}   —— 有 ×N 计数说明同类被合并过"
  echo "  · ERROR 条数：${ERRN:-0}"
  {
    echo "re-top 最近 8 条："; grep 're-top' "$LOG" | tail -8
    echo "ERROR 最近 8 条："; grep ' ERROR' "$LOG" | tail -8
    echo "抬层失败探测器（应为空）："; grep 'raise tool window failed' "$LOG" | tail -5
  } >> "$REPORT"
  grep 're-top' "$LOG" | tail -8 | sed 's/^/      /'
  echo "  完整摘要已写到：$REPORT"
else
  echo "没找到运行日志：$LOG"
fi

# ---------- 4. 三问：只有人能回答的手感 ----------
if [ "$MODE" = "--check-only" ]; then
  line
  echo "（--check-only：跳过交互三问，只做自动取证）"
  exit 0
fi
line
echo "下面三问请用**正常手感**回答：1 = 明显不对，2 = 有一点，3 = 没有。"
echo "（回答会记进报告文件，回传给开发侧即可）"

ask() {
  printf '%s\n' "$1"
  printf '  1) 明显有问题   2) 有一点   3) 没有   输入：'
  read -r ans
  printf '%s → %s\n' "$1" "${ans:-未答}" >> "$REPORT"
}

ask "Q1 打开「目标价」面板后拖动主窗：窗口有没有跟不上手、或背景（毛玻璃）掉队/闪一下？"
ask "Q2 拖主窗底部边缘改高度：松手后有没有'还在继续长/缩'的尾巴？"
ask "Q3 在面板里连续打字：有没有某个字符明显慢半拍、或整窗卡一下？"

# ---------- 5. 实验开关 system_drag ----------
line
echo "实验开关 system_drag（把窗口位移交给 macOS 窗口服务器，v1.4.29 默认关闭）"
CUR="$(launchctl getenv STOCKFLOAT_FEATURES 2>/dev/null)"
echo "  当前环境变量 STOCKFLOAT_FEATURES = ${CUR:-（未设置，即默认关闭）}"
printf '  要不要现在打开它再复验一遍？(y/N)：'
read -r ON
case "$ON" in
  y|Y)
    launchctl setenv STOCKFLOAT_FEATURES "system_drag"
    echo "  已开启。请重启程序（右键菜单退出后再打开）后重跑本脚本，回答上面三问。"
    echo "  还原：运行本脚本加参数 --off；或 launchctl unsetenv STOCKFLOAT_FEATURES"
    echo "  操作：开启 system_drag 后复验三问" >> "$REPORT"
    ;;
  *)
    echo "  保持默认（关闭）。"
    ;;
esac

line
echo "复验完成。把这份文件回传即可：$REPORT"
echo "要关掉实验开关：$0 --off"
