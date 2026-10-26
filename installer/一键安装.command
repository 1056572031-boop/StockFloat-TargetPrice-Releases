#!/bin/sh
# 装进 DMG 根目录、让用户双击的那一个安装脚本。
#
# 为什么需要它（v1.4.57 之后实测定的）：
#   本程序是 ad-hoc 签名、不做公证。用户用浏览器/微信把 DMG 拿到手的那一刻，DMG 会被
#   贴上"来历标签"（com.apple.quarantine）；而实测（tools/verify_install_paths.py 的
#   R2/R3 两条）：**卷里的文件本身不带标签，标签是在把 .app 拖进「应用程序」那一瞬间
#   传染过去的**。所以这道墙只在第一次安装出现一次，OTA 更新（程序自己下载 zip）不出现。
#   本脚本把"拷贝 + 清掉自己刚拷出来那一份的标签"合进一次双击，用户不必打开终端。
#
# 文件名 2026-10-12 起改成中文（用户口径：要双击的那一枚得一眼看懂是干什么的）。
# 原来这条约束写的是"刻意 ASCII：中文文件名在挂载卷里撞 NFC/NFD，find/glob 安静返回空"。
# 改之前按 HFS+ 真卷实测过（hdiutil create -fs HFS+ -format UDZO，与发版链同参数）：
# 卷里 readdir 回来的「一键安装.command」NFC 与 NFD **同形**（CJK 表意文字没有可分解的字符），
# glob / find -name / pathlib.exists 三种找法全部命中，按中文名执行也正常（$0 的 dirname
# 仍是挂载卷）。所以那四个字撞不到坑。
# ⚠ 结论只对"纯中文 + ASCII"成立。名字里一旦混进会变音符的拉丁字母（é/å/ñ）或谚文，
#   HFS+ 存的是 NFD 而源码字面量是 NFC，find/glob 就会**安静返回空**，测出来的是假绿 ——
#   那条坑是真的，只是不在这四个字上。改这个名字前先跑那三种找法。
# 名字的唯一来源就是**文件名本身**：发版脚本与核验工具都按 installer/*.command 发现它，
# 不再有任何一处写死全名（写死三处、改一处 = 卷里静默没有安装脚本，用户只剩拖拽那条墙）。
#
# ⚠ 写这个文件时的两条硬写法（2026-10-13 实测，AGENTS §5.6）：
#   ① **变量后面紧跟中文标点必须写 ${VAR}**。`"$DEST，"` 在 UTF-8 locale 下会被 bash 3.2
#      把全角逗号的首字节吞进变量名，`set -u` 当场以「DEST\xEF: unbound variable」终止 ——
#      而用户终端是 LANG=zh_CN.UTF-8、这台机器的 shell 没有设 LANG，所以关卡永远复现不了。
#      判据：tools/shell_cjk_scan.py（全仓 .sh/.command 硬零）+ 核验器整体跑在 UTF-8 locale。
#   ② **任何破坏动作之前先验完该验的**。旧版被移走之后才做的校验，失败时用户手上
#      既没有新版也没有旧版（实测过一次：坏包 → 「应用程序」里躺着坏的那一份，
#      能用的那份在废纸篓里）。所以这里的顺序是：验源包 → 验可写 → 验没在跑 → 才动旧版，
#      动了之后每条失败路径都把旧版搬回原位。
#
# 四条硬约束（FR-12）：不用 sudo、不递归别人的目录、不含任何删除动作（旧版一律 mv 走）、
# 失败必须打印下一步。SF_INSTALL_DIR / SF_INSTALL_NO_LAUNCH / SF_INSTALL_WAIT_SEC /
# SF_INSTALL_ALLOW_RUNNING 四个环境变量只给核验工具用（它把目标目录换到临时目录、
# 避免在用户屏幕上拉起程序、并把"等旧版退出"那一档压到几秒；最后那个只给
# "本条路线不测在跑这一件事"的路线用，见 R2c 才是真测它的）。

set -u

SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="${SF_INSTALL_DIR:-/Applications}"
APP_NAMES="Skyrocket.app 实时股价-目标价版.app"
# bundle id 两代都认（与 updater/update_helper.sh 同一口径）：卷里那一份不是我们自己的
# 包时，宁可不装也别把它放进「应用程序」。
BUNDLE_IDS="com.skyrocket.app com.stockfloat.targetprice"
STAMP="$(date +%Y%m%d-%H%M%S)"
PLIST=/usr/libexec/PlistBuddy
# 等旧版退出的上限（秒）。绝不 SIGKILL —— 与助手同一条理由：
# 强杀会截断「退出时保存配置」，用户丢的是自己录的目标价。
WAIT_SEC="${SF_INSTALL_WAIT_SEC:-60}"
# 别让终端的工作目录占住挂载卷（最后那一步弹出卷才做得动）。
cd / || true

SRC=""
BASE=""
DEST=""
STAGE=""
TRASHED=""
OLD_MOVES=""
LOG=""
LOCK_DIR=""

say() {
  printf '%s\n' "$1"
  if [ -n "$LOG" ]; then
    printf '%s\n' "$1" >>"$LOG" 2>/dev/null || true
  fi
}

die_next_step() {
  # 死路是最坏的结局：用户站在这儿既装不上也不知道下一步找谁。
  say ""
  say "  ✗ 安装没有完成：$1"
  say "  下一步：查看随包 README 的反馈入口；诊断包可能含股票代码与日志，发送前请自行检查："
  say "        在程序已能打开的情况下：右键悬浮窗 →「导出诊断包」→ 把桌面上的 zip 发来"
  say "        还打不开的情况：把这页终端截图发来，或直接发安装日志这个文件"
  if [ -n "$LOG" ]; then
    say "      安装日志：$LOG"
  fi
  say "      时间戳 $STAMP / 来源卷 $SELF_DIR / 目标目录 $INSTALL_DIR"
  exit 1
}

# 环境变量只给核验器使用，但仍必须当作不可信输入处理：负数会跳过等待，
# 非数字会在 set -u 下把错误变成一条用户看不懂的 shell 报错。
case "$WAIT_SEC" in
  ''|*[!0-9]*) die_next_step "等待旧版退出的时间不是有效的非负整数：${WAIT_SEC}" ;;
esac
if [ "${#WAIT_SEC}" -gt 4 ]; then
  die_next_step "等待旧版退出的时间过长（最多 3600 秒）：${WAIT_SEC}"
fi
if [ "$WAIT_SEC" -gt 3600 ]; then
  die_next_step "等待旧版退出的时间过长（最多 3600 秒）：${WAIT_SEC}"
fi

# 双击两次是很容易发生的操作。mkdir 是 macOS 自带 shell 下可用的原子锁，
# 避免两个实例同时搬走旧包、互相覆盖 DEST。锁目录保持为空，正常退出和错误退出
# 用 rmdir 释放，不引入任何删除用户文件的动作；强制断电留下的锁由用户按提示处理。
acquire_install_lock() {
  LOCK_KEY="$(printf '%s' "$INSTALL_DIR" | /usr/bin/cksum | /usr/bin/awk '{print $1}')"
  LOCK_DIR="${TMPDIR:-/tmp}/skyrocket-install-${UID:-0}-${LOCK_KEY}.lock"
  if ! /bin/mkdir "$LOCK_DIR" 2>/dev/null; then
    die_next_step "已有另一场安装正在进行（或上一次安装被强制中断）；请等它结束后再重试，必要时移除空锁目录 ${LOCK_DIR}"
  fi
  trap 'if [ -n "${LOCK_DIR:-}" ]; then /bin/rmdir "$LOCK_DIR" 2>/dev/null || true; fi' EXIT
  trap 'exit 130' HUP INT TERM
}

# 旧版已经被移走、而后面某一步失败时：把坏的那一份挪开、把每一代旧版放回原位。
# 一律 mv、一律不删；放回原位之后用户至少还有上一次能用的程序。
restore_old_app() {
  if [ -n "$STAGE" ] && [ -d "$STAGE" ]; then
    STAGE_BROKEN="$HOME/.Trash/${BASE}.${STAMP}.broken"
    /bin/mv "$STAGE" "$STAGE_BROKEN" 2>/dev/null \
      && say "  未完成的暂存包已移到废纸篓：$(basename "$STAGE_BROKEN")" \
      || say "  ⚠ 未完成的暂存包没能移到废纸篓：${STAGE}（可以手动移走）"
    STAGE=""
  fi
  [ -n "$OLD_MOVES" ] || return 0
  if [ -d "$DEST" ]; then
    BROKEN="$HOME/.Trash/${BASE}.${STAMP}.broken"
    /bin/mv "$DEST" "$BROKEN" 2>/dev/null \
      && say "  刚才拷进去的那一份已移到废纸篓：$(basename "$BROKEN")（没有删除）" \
      || say "  ⚠ 刚才拷进去的那一份没能移到废纸篓：${DEST}（可以手动拖进废纸篓）"
  fi
  while IFS="$(printf '\t')" read -r old_dest old_keep; do
    [ -n "$old_dest" ] && [ -d "$old_keep" ] || continue
    if /bin/mv "$old_keep" "$old_dest" 2>/dev/null; then
      say "  旧版已放回原位：${old_dest}（你原来那个版本还能照常打开）"
    else
      say "  ⚠ 旧版没能自动放回原位，请手动把它从废纸篓拖回：${old_keep}"
    fi
  done <<EOF
$OLD_MOVES
EOF
  OLD_MOVES=""
}

# BEGIN close_install_terminal
close_install_terminal() {
  [ "${TERM_PROGRAM:-}" = "Apple_Terminal" ] || return 0
  [ -t 0 ] || return 0
  INSTALL_TTY="$(/usr/bin/tty 2>/dev/null)" || return 0
  case "$INSTALL_TTY" in /dev/ttys[0-9]*) ;; *) return 0 ;; esac
  # 脱离控制终端再等待，避免关窗助手自己被 Terminal 算成仍在运行的命令。
  # 只关已结束的单标签安装窗口；手动执行或合并了其他标签时保留窗口。
  /usr/bin/perl -MPOSIX -e 'POSIX::setsid() >= 0 or die "setsid: $!"; exec @ARGV or die "exec: $!";' \
    /usr/bin/osascript -l JavaScript - "$INSTALL_TTY" \
    <<'INSTALL_TERMINAL_JXA' >>"${LOG:-/dev/null}" 2>&1 &
function run(argv) {
    return closeInstallWindow(Application("com.apple.Terminal"), argv[0], delay);
}
function closeInstallWindow(terminal, installTTY, pause) {
    if (!terminal.running()) return "terminal-not-running";
    var targetID = null;
    var windows = terminal.windows();
    for (var i = 0; i < windows.length; i++) {
        var tabs = windows[i].tabs();
        if (tabs.length === 1 && tabs[0].tty() === installTTY) {
            targetID = windows[i].id();
            break;
        }
    }
    if (targetID === null) return "no-dedicated-install-window";
    for (var attempt = 0; attempt < 30; attempt++) {
        if (!terminal.running()) return "terminal-not-running";
        windows = terminal.windows();
        var target = null;
        for (var j = 0; j < windows.length; j++) {
            if (windows[j].id() === targetID) target = windows[j];
        }
        if (target === null) return "install-window-already-closed";
        target = terminal.windows.byId(targetID);
        tabs = target.tabs();
        if (tabs.length !== 1 || tabs[0].tty() !== installTTY) return "install-window-changed";
        if (!tabs[0].busy() && tabs[0].processes().length === 0) {
            target.close({saving: "no"});
            return "install-window-closed";
        }
        pause(0.2);
    }
    return "install-window-still-in-use";
}
INSTALL_TERMINAL_JXA
  return 0
}
# END close_install_terminal

say "Skyrocket 安装（这一步不需要你打开终端以外的任何东西）"
say "------------------------------------------------------------"

# 1) 在本卷里找要装的 app（两代产品名都认）
for name in $APP_NAMES; do
  if [ -d "$SELF_DIR/$name" ]; then
    SRC="$SELF_DIR/$name"
    break
  fi
done
[ -n "$SRC" ] || die_next_step "在这个卷里没找到 Skyrocket.app（DMG 可能没挂载完整，重新双击一次桌面上的 .dmg）"

BASE="$(basename "$SRC")"
PRODUCT="${BASE%.app}"
DEST="$INSTALL_DIR/$BASE"
STAGE="$INSTALL_DIR/.${BASE}.${STAMP}.installing.app"

# 日志落在用户自己的 ~/Library/Logs：装不上时要发出去的就是这个文件，
# 而不是"把这一屏截图发来"（截图里丢掉的时间戳与路径，正是每次排查最缺的东西）。
LOG_DIR="$HOME/Library/Logs"
if /bin/mkdir -p "$LOG_DIR" 2>/dev/null; then
  LOG="$LOG_DIR/${PRODUCT}-安装-${STAMP}.log"
  : >"$LOG" 2>/dev/null || LOG=""
fi

# 2) 卷里那一份的身份：版本要报给用户，包标识必须是我们自己的两代之一。
PLIST_PATH="$SRC/Contents/Info.plist"
[ -f "$PLIST_PATH" ] || die_next_step "${BASE} 里缺少 Info.plist，这个 DMG 不完整（重新下载一份）"
VERSION="$("$PLIST" -c 'Print :CFBundleShortVersionString' "$PLIST_PATH" 2>/dev/null || true)"
IDENTIFIER="$("$PLIST" -c 'Print :CFBundleIdentifier' "$PLIST_PATH" 2>/dev/null || true)"
ID_OK=0
for want in $BUNDLE_IDS; do
  [ "$IDENTIFIER" = "$want" ] && ID_OK=1
done
[ "$ID_OK" = "1" ] || die_next_step "卷里那个包的标识是「${IDENTIFIER:-（读不到）}」，不是本程序的两代标识（${BUNDLE_IDS} ），这个 DMG 不是我们发的那一份"
say "  找到安装包：${BASE}（v${VERSION:-未知}，标识 ${IDENTIFIER}）"

acquire_install_lock

# 3) 先验**源包**完整，再动用户机器上的任何东西。
#    这条排在移动旧版之前是有原因的：校验不过就是"包在路上坏了"，此刻用户手上
#    那个旧版还完整地待在「应用程序」里；换到拷贝之后做，失败时就只剩一个坏包了。
if ! /usr/bin/codesign --verify --deep --strict "$SRC" 2>/dev/null; then
  die_next_step "DMG 里的包体校验没过（多半是下载中断或被改动过），所以什么都没动 —— 重新下载一份再来一次"
fi
say "  源包完整（codesign 校验通过）"

# 4) 目标目录与废纸篓都必须可用，先问清楚再动手。
if [ ! -d "$INSTALL_DIR" ] || [ ! -w "$INSTALL_DIR" ]; then
  die_next_step "${INSTALL_DIR} 不可写：这台机器上的「应用程序」需要管理员权限才能装东西（换一个管理员账户，或让管理员先把旧版本拖进废纸篓）"
fi
if ! /bin/mkdir -p "$HOME/.Trash" 2>/dev/null || [ ! -w "$HOME/.Trash" ]; then
  die_next_step "用户废纸篓不可写，无法在失败时安全保留旧版；请先修复 ${HOME}/.Trash 的权限后重试"
fi

# 5) 有没有一份正在运行 —— 这一步不做，装完跑着的**还是旧版**。
#    实测：旧版在跑时点「一键安装」，脚本一路报成功，而机器上活着的仍是旧一代载荷，
#    新起的那一代会在 sf_logging._become_single_instance() 处 sys.exit(0) 静默退出；
#    而被移进废纸篓的旧版**仍在运行**。用户看到的就是"装了新版问题怎么还在"。
#    只等不杀：等用户从悬浮窗右键「退出」，最多 ${WAIT_SEC} 秒。
#    pgrep -f 按整条命令行做子串匹配，别的进程的命令行里恰好带着这串路径
#    （另一个终端里的 tail / less / 外层 shell -c 都实测命中过）也会被当成"程序在运行"，
#    于是装不下去还指向一个并不存在的悬浮窗。所以命中之后再逐个核对：这个进程是不是真的
#    以包内可执行文件启动的 —— 命令行去掉开头的解释器（sh/bash/zsh/dash）之后，
#    必须以绝对路径开头，且包路径之前不能再夹着别的参数。拿不准时按"在运行"处理：
#    多等一会儿只是慢，漏判则会在旧版还活着时就开始替换。
is_app_process() {
  # $1 = pid，$2 = 包名（Skyrocket.app 等）
  _args="$(/bin/ps -p "$1" -o args= 2>/dev/null || true)"
  [ -n "$_args" ] || return 1
  case "$_args" in
    "/bin/sh "*) _args="${_args#/bin/sh }" ;;
    "/bin/bash "*) _args="${_args#/bin/bash }" ;;
    "/bin/zsh "*) _args="${_args#/bin/zsh }" ;;
    "/bin/dash "*) _args="${_args#/bin/dash }" ;;
  esac
  _head="${_args%%"${2}"/Contents/MacOS/*}"
  case "$_head" in
    /*) ;;
    *) return 1 ;;
  esac
  case "$_head" in
    *" /"*|*" -"[A-Za-z]*) return 1 ;;
  esac
  return 0
}

running_app_pids() {
  _found=""
  for _name in $APP_NAMES; do
    for _pid in $(/usr/bin/pgrep -f "${_name}/Contents/MacOS/" 2>/dev/null || true); do
      if is_app_process "$_pid" "$_name"; then
        _found="$_found $_pid"
      fi
    done
  done
  printf '%s' "$_found"
}

if [ "${SF_INSTALL_ALLOW_RUNNING:-0}" != "1" ]; then
  RUNNING="$(running_app_pids)"
  if [ -n "$RUNNING" ]; then
    say "  检测到本程序正在运行（pid ${RUNNING} ）——请先右键悬浮窗 →「退出」"
    say "  我会等你退出（最多 ${WAIT_SEC} 秒），退出之后自动继续装，不需要你再双击一次。"
    waited=0
    STILL="$RUNNING"
    while [ "$waited" -lt "$WAIT_SEC" ]; do
      /bin/sleep 1
      waited=$((waited + 1))
      STILL="$(running_app_pids)"
      [ -n "$STILL" ] || break
    done
    if [ -n "$STILL" ]; then
      die_next_step "旧版一直没能退出，所以什么都没动 —— 右键悬浮窗 →「退出」之后，重新双击这个卷里的「一键安装」即可"
    fi
    say "  旧版已退出，继续安装"
  fi
fi

# 6) 先复制到隐藏暂存包。正式路径在校验完成前保持原样，避免留下半拷贝包。
if [ -e "$STAGE" ]; then
  die_next_step "安装目录里已有未完成的暂存包：${STAGE}（请确认没有另一场安装后移走它，再重试）"
fi
if ! /usr/bin/ditto "$SRC" "$STAGE"; then
  restore_old_app
  die_next_step "复制到安装暂存区失败（如果是磁盘或权限问题，把安装日志这个文件发给开发者）"
fi
say "  已复制到暂存区，正式版本尚未替换"

# 7) 在暂存包上清标签并验签，验不过时旧版仍在原位。
/usr/bin/xattr -dr com.apple.quarantine "$STAGE" 2>/dev/null || true
if /usr/bin/xattr -rl "$STAGE" 2>/dev/null | /usr/bin/grep -q "com.apple.quarantine"; then
  restore_old_app
  die_next_step "来历标签没能全部清掉（这一步失败通常是磁盘或权限异常），旧版未被动过"
fi
if ! /usr/bin/codesign --verify --deep --strict "$STAGE" 2>/dev/null; then
  restore_old_app
  die_next_step "暂存包签名校验没过，旧版未被动过；请重新下载一份安装包"
fi
say "  暂存包完整（标签与签名校验通过）"

# 8) 校验完成后才移动旧版；两代名称都要迁移，后续失败统一回滚。
# 新旧包可同时存在于 /Applications：这种升级不能只看 DEST（新版名），否则旧版
# 会遗留成第二个可启动图标。每一代先各自移到废纸篓，完成后才放新版到 DEST。
for old_name in $APP_NAMES; do
  old_dest="$INSTALL_DIR/$old_name"
  [ -d "$old_dest" ] || continue
  old_keep="$HOME/.Trash/${old_name}.${STAMP}.old"
  if ! /bin/mv "$old_dest" "$old_keep" 2>/dev/null; then
    restore_old_app
    die_next_step "旧版本来在 ${old_dest}，但没能移到废纸篓（检查这个目录的权限，或手动把它拖进废纸篓后重试）"
  fi
  OLD_MOVES="${OLD_MOVES}${old_dest}$(printf '\t')${old_keep}
"
  say "  旧版已移到废纸篓：$(basename "$old_keep")（不是删除，随时能拖回来）"
done

# 9) 原子地把已验证的暂存包放到正式路径。
if ! /bin/mv "$STAGE" "$DEST"; then
  restore_old_app
  die_next_step "已验证的暂存包没能切换到 ${DEST}，旧版已尝试放回原位"
fi
STAGE=""
say "  已装入：${DEST}"

say "  安装完成：${PRODUCT} v${VERSION:-未知}"
[ -n "$LOG" ] && say "  安装日志：$LOG"

# 10) 启动
if [ "${SF_INSTALL_NO_LAUNCH:-0}" = "1" ]; then
  say "  （核验模式：不启动程序，SF_INSTALL_NO_LAUNCH=1）"
  exit 0
fi
say "  正在打开 Skyrocket……第一次就能打开，不需要再去系统设置里点什么。"
/usr/bin/open "$DEST" || die_next_step "程序装好了但没能启动（把安装日志这个文件发给开发者）"

# 11) 装完顺手把那个白色磁盘图标从桌面上拿走（尽力而为，做不到也不报错 ——
#     此刻安装已经完成，弹不掉卷只是不好看，不该把成功说成失败）。
case "$SELF_DIR" in
  /Volumes/*) /usr/bin/hdiutil detach "$SELF_DIR" >/dev/null 2>&1 || true ;;
esac
close_install_terminal
exit 0
