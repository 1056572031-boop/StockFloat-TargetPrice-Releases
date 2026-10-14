#!/bin/sh
# 临时目录旧副本清理（双击运行，面向没有本仓库的机器，如 iMac 实机）
#
# 解决什么：2026-09-22 / 09-23 的"程序意外退出"弹窗，崩溃路径都是
#   /private/tmp/*/实时股价-目标价版.app —— 不是「应用程序」里的正式版，
#   而是历史验证遗留在临时目录的 v1.3.97 旧副本被系统重新拉起。
#   旧副本没有 v1.4.5 起的启动守卫，一启动就在 Qt 导入期崩溃，
#   所以只能让它不再被当成应用：改名去掉 .app 后缀。
#
# 安全边界：只扫系统临时目录（不碰「应用程序」）；只处理本程序的包标识；
#   正在运行的副本跳过；全程只改名、不删除，去掉 .stale 后缀即可还原。
#
# ⚠ 身份必须**两代都认**（2026-10-12 全方位复核 F2 修的就是这一处）：
#   v1.4.55 程序改名 Skyrocket / com.skyrocket.app，而本工具当时只写了旧名 ——
#   结果是装机版机器上它打印一句「应用程序未安装本程序」然后 exit 0 什么都不做，
#   而 AGENTS §3.1 恰恰把这里列为"没有仓库的机器上唯一手段"。
#   本工具刻意自包含（不 import 仓库里的 Python），所以名单写在这里；
#   它与 sf_constants 是否同步由 tests/test_standalone_command_tools.py 钉住 ——
#   改 sf_constants 而忘了改这里，那条用例会红，不会再靠人记。

BUNDLE_IDS="com.skyrocket.app com.stockfloat.targetprice"
APP_NAMES="Skyrocket.app 实时股价-目标价版.app"
LIST="/tmp/sf_sweep_list.$$"

echo "=============================================="
echo " 实时股价 / Skyrocket · 临时目录旧副本清理"
echo "=============================================="

INSTALLED=""
for CAND in $APP_NAMES; do
  if [ -d "/Applications/$CAND" ]; then INSTALLED="/Applications/$CAND"; break; fi
done
if [ -n "$INSTALLED" ]; then
  IV="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' \
        "$INSTALLED/Contents/Info.plist" 2>/dev/null)"
  echo "「应用程序」正式版：$INSTALLED (v$IV)"
  case "$IV" in
    1.3.*|1.4.[0-4])
      echo "  ⚠ 该版本尚无启动守卫，建议在本机浮窗右键菜单里检查更新到最新版。"
      ;;
  esac
else
  echo "「应用程序」未安装本程序 —— 请先安装正式版，再运行本工具。"
  echo "   （认的是这两代名字：${APP_NAMES}）"
  exit 0
fi

# 正在运行的副本路径（避免误改正在使用的那一份）
ps -Ao comm= 2>/dev/null | grep -F ".app/Contents/" | sed 's/\.app\/Contents\/.*$/.app/' \
  | sort -u > "$LIST.running" 2>/dev/null || : > "$LIST.running"

: > "$LIST"
for ROOT in /private/tmp /private/var/tmp "$TMPDIR" /private/var/folders; do
  [ -n "$ROOT" ] && [ -d "$ROOT" ] || continue
  find "$ROOT" -maxdepth 6 -type d -name "*.app" 2>/dev/null >> "$LIST"
done

FOUND=0
CHANGED=0
while IFS= read -r APP; do
  [ -n "$APP" ] || continue
  case "$APP" in
    /Applications/*) continue ;;
  esac
  ID="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' \
        "$APP/Contents/Info.plist" 2>/dev/null | tr -d ' \t\r\n')"
  NAME="$(basename "$APP")"
  MATCH=0
  for WANTED in $BUNDLE_IDS; do
    if [ "$ID" = "$WANTED" ]; then MATCH=1; break; fi
  done
  if [ "$MATCH" -eq 0 ] && [ -z "$ID" ]; then
    # Info.plist 已被回收，只剩半具副本 —— 正是崩溃报告里那种形态：按目录名认两代
    for WANTED in $APP_NAMES; do
      if [ "$NAME" = "$WANTED" ]; then MATCH=1; break; fi
    done
  fi
  [ "$MATCH" -eq 1 ] || continue
  # 必须真有可执行文件才算"还能被启动"，空壳目录不动
  if [ -z "$(ls -A "$APP/Contents/MacOS" 2>/dev/null | head -1)" ]; then
    continue
  fi
  REAL="$(cd "$APP" 2>/dev/null && pwd -P)" || continue
  if grep -Fxq "$REAL" "$LIST.running" 2>/dev/null; then
    echo "跳过（正在运行）：$APP"
    continue
  fi
  FOUND=$((FOUND + 1))
  VER="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' \
         "$APP/Contents/Info.plist" 2>/dev/null)"
  [ -n "$VER" ] || VER="未知"
  TARGET="$APP.stale"
  N=1
  while [ -e "$TARGET" ]; do TARGET="$APP.stale.$N"; N=$((N + 1)); done
  if mv "$APP" "$TARGET" 2>/dev/null; then
    CHANGED=$((CHANGED + 1))
    echo "已中性化：$APP  (v$VER)"
  else
    echo "改名失败：$APP"
  fi
done < "$LIST"

rm -f "$LIST" "$LIST.running" 2>/dev/null

echo "----------------------------------------------"
if [ "$FOUND" -eq 0 ]; then
  echo "未发现临时目录里的旧副本，无需处理。"
  echo "若仍弹「程序意外退出」，请把弹窗里的版本号和路径发来，另查原因。"
else
  echo "共处理 $CHANGED / $FOUND 个旧副本（只改名、未删除）。"
  echo "以后不会再出现这些旧副本的崩溃弹窗；正式版不受影响。"
fi
echo "=============================================="
