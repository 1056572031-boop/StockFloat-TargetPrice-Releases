#!/bin/sh
# 外部更新助手：等待主程序退出后，预检 ZIP、替换 .app；失败则回退旧版。
set -eu

PID=""
ARCHIVE=""
EXPECTED_SHA256=""
EXPECTED_VERSION=""
TARGET=""
RESULT=""
HANDOFF=""
VERIFICATION_HOME=""

# 当前使用固定自签名、未使用 Developer ID 公证。若未来启用 Developer ID 签名，
# 须同时填写两项并随发布证书轮换更新；当前留空仍执行基础 codesign 校验，绝不伪造身份锚定。
EXPECTED_TEAM_ID=""
EXPECTED_CERT_SHA1=""
MAX_UNPACK_BYTES=$((2 * 1024 * 1024 * 1024))
MAX_UNPACK_FILES=20000
MIN_FREE_BYTES=$((32 * 1024 * 1024))
EXTRACT_TIMEOUT=90

while [ "$#" -gt 0 ]; do
  case "$1" in
    --pid|--archive|--sha256|--version|--target|--result|--handoff|--team-id|--cert-sha1)
      [ "$#" -ge 2 ] || exit 64
      case "$1" in
        --pid) PID="$2" ;;
        --archive) ARCHIVE="$2" ;;
        --sha256) EXPECTED_SHA256="$2" ;;
        --version) EXPECTED_VERSION="$2" ;;
        --target) TARGET="$2" ;;
        --result) RESULT="$2" ;;
        --handoff) HANDOFF="$2" ;;
        --team-id) EXPECTED_TEAM_ID="$2" ;;
        --cert-sha1) EXPECTED_CERT_SHA1="$2" ;;
      esac
      shift 2
      ;;
    --verification-home)
      [ "$#" -ge 2 ] && [ -n "$2" ] && [ -z "$VERIFICATION_HOME" ] || exit 64
      VERIFICATION_HOME="$2"; shift 2 ;;
    *) exit 64 ;;
  esac
done

write_result() {
  # ⚠ 这两个局部名**必须**带 wr_ 前缀，不许借用 parent 或 tmp（v1.4.61 实测修的那枚）：
  # 这两个名字在外层是"安装目标所在目录"与"解包工作目录"。原先这里把 parent 赋成
  # 「dirname 取 result 的路径」，于是每写一次 result 就把全局 parent 改到了
  # result.json 所在目录 —— 走完 installed 之后 prune_rollbacks 拿这个 parent 去扫
  # 回退包，扫的是临时目录，一条也扫不到，回退包于是永远留在「应用程序」里
  # （实测：三份回退包全留着，prune 目录是空的，而且零报错）。
  # 别把那一行的**原文**抄回这段注释：发版链 3.6 的 forbidfile 是对包内文件做子串搜索的，
  # 注释里出现原句就等于"旧写法仍在包里"（本轮就是被自己这条闸门拦下来的）。
  # 抽出来单独跑的测法测不到它 —— 它自己传 parent，从没走过"先写 result 再清盘"这个
  # 真实顺序。所以正向路线必须整条跑（tests/test_update_helper.py::TestHelperPositiveRoute）。
  status="$1"
  message="$2"
  wr_dir="$(dirname "$RESULT")"
  mkdir -p "$wr_dir"
  umask 077
  wr_tmp="$RESULT.tmp-$$"
  printf '{"status":"%s","message":"%s"}\n' "$status" "$message" > "$wr_tmp"
  mv -f "$wr_tmp" "$RESULT"
}

fail() {
  write_result "failed" "$1"
  exit 1
}

# 验证档只接受系统临时区内同一私有根目录，不信任调用者的 TMPDIR。
# 每次启动前重验：拒绝目录或派生文件被换成软链接；不改变生产启动语义。
validate_verification_home() {
  /usr/bin/perl -MCwd -e '
    use strict; use warnings;
    my ($home, $target) = @ARGV;
    sub checked_dir {
      my ($p) = @_;
      $p =~ m{^/} && $p !~ m{(?:^|/)[.][.]?(?:/|$)|[\r\n]} or exit 1;
      $p =~ s{^/tmp(?=/|$)}{/private/tmp};
      $p =~ s{^/var(?=/|$)}{/private/var};
      $p =~ s{/+$}{};
      my $part = "";
      for my $name (split m{/}, $p) {
        next if $name eq "";
        $part .= "/" . $name;
        -l $part and exit 1;
      }
      -d $p or exit 1;
      my $real = Cwd::realpath($p);
      defined $real && $real eq $p or exit 1;
      return $real;
    }
    $home = checked_dir($home);
    $target = checked_dir($target);
    my $user_tmp = qx{/usr/bin/getconf DARWIN_USER_TEMP_DIR 2>/dev/null};
    chomp $user_tmp;
    my @bases = ("/private/tmp", "/private/var/tmp");
    push @bases, Cwd::realpath($user_tmp) if -d $user_tmp;
    # 私有根 = home 的直接父目录，且它必须严格落在某个临时基目录之下（不能就是基目录本身）。
    # 调用方的布局是 <根>/home 与 <根>/Skyrocket.app 并列（tools/verify_candidate_delivery.py）。
    # 不再取「基目录之后的第一段」：TMPDIR 被指到更深一层时，那一段会把同层的两个不同临时
    # 目录判成同一个根，跨目录的 target 就不再被拒（2026-10-22 实测，T-26）。
    my $root = $home;
    $root =~ s{/[^/]+$}{};
    my $under_base = 0;
    for my $base (@bases) {
      next unless defined $base;
      if (index($root, "$base/") == 0) { $under_base = 1; last; }
    }
    $under_base && index($target, "$root/") == 0 or exit 1;
    for my $name (qw(config.json universe.json last_quote.json debug.log single.sock)) {
      my $p = "$home/$name";
      -l $p and exit 1;
      (!-e $p || !-d $p) or exit 1;
    }
    print $home;
  ' "$VERIFICATION_HOME" "$TARGET" 2>/dev/null
}

launch_app() {
  if [ -z "$VERIFICATION_HOME" ]; then
    # 带 --handoff 时，助手要等新版自己写下的启动确认；新版只能从环境变量
    # SF_UPDATE_HANDOFF 拿到令牌。这里原先只写 open "$1"，令牌到不了新版，
    # 于是每次装好的新版都在确认超时后被回退（用户反馈：更新后重开又变回旧版）。
    # --env 要求新起进程，所以带 -n；旧实例此刻已退出（上面等过主程序 PID）。
    if [ -n "$HANDOFF" ]; then
      /usr/bin/open -n --env "SF_UPDATE_HANDOFF=$HANDOFF" "$1"
      return $?
    fi
    /usr/bin/open "$1"
    return $?
  fi
  # 回退包恢复到 TARGET 后才启动；不允许按 bundle id 启动其他已安装副本。
  [ "$1" = "$TARGET" ] || return 1
  vh_checked="$(validate_verification_home)" || return 1
  VERIFICATION_HOME="$vh_checked"
  if [ -n "$HANDOFF" ]; then
    /usr/bin/open -n -g \
      --env "SF_CONFIG_PATH=$VERIFICATION_HOME/config.json" \
      --env "SF_UNIVERSE_PATH=$VERIFICATION_HOME/universe.json" \
      --env "SF_LAST_QUOTE_PATH=$VERIFICATION_HOME/last_quote.json" \
      --env "SF_LOG_PATH=$VERIFICATION_HOME/debug.log" \
      --env "SF_SINGLE_SOCK=$VERIFICATION_HOME/single.sock" \
      --env SF_ALLOW_TEMP_LAUNCH=1 \
      --env QT_QPA_PLATFORM=offscreen \
      --env SF_SEED_FIRST_SCREEN=0 \
      --env "SF_UPDATE_HANDOFF=$HANDOFF" "$1"
  else
    /usr/bin/open -n -g \
      --env "SF_CONFIG_PATH=$VERIFICATION_HOME/config.json" \
      --env "SF_UNIVERSE_PATH=$VERIFICATION_HOME/universe.json" \
      --env "SF_LAST_QUOTE_PATH=$VERIFICATION_HOME/last_quote.json" \
      --env "SF_LOG_PATH=$VERIFICATION_HOME/debug.log" \
      --env "SF_SINGLE_SOCK=$VERIFICATION_HOME/single.sock" \
      --env SF_ALLOW_TEMP_LAUNCH=1 \
      --env QT_QPA_PLATFORM=offscreen \
      --env SF_SEED_FIRST_SCREEN=0 "$1"
  fi
}

rollback_or_fail() {
  reason="$1"
  if [ -d "$TARGET" ]; then
    /bin/mv "$TARGET" "$work/installed_new_failed.app" 2>/dev/null || true
  fi
  if /bin/mv "$rollback" "$TARGET" 2>/dev/null; then
    write_result "rolled_back" "$reason"
    launch_app "$TARGET" >/dev/null 2>&1 || true
    exit 1
  fi
  write_result "rollback_failed" "$reason"
  exit 1
}

[ -n "$PID" ] && [ -n "$ARCHIVE" ] && [ -n "$EXPECTED_SHA256" ] && [ -n "$EXPECTED_VERSION" ] && [ -n "$TARGET" ] && [ -n "$RESULT" ] || fail "invalid_arguments"
case "$PID" in
  0|*[!0-9]*) fail "invalid_arguments" ;;
esac
if [ -n "$HANDOFF" ]; then
  # 十六进制字符逐个列出，不写成 a-f 区间：macOS 自带 /bin/sh（bash 3.2）在 UTF-8 区域设置下
  # 按排序规则展开区间，大写 A-F 会被一并放行（公开 CI 的 UTF-8 环境实测），而本机默认没设
  # 区域，走 C 分支看不出来。
  case "$HANDOFF" in
    *[!0123456789abcdef]* ) fail "handoff_invalid" ;;
  esac
  [ "${#HANDOFF}" -eq 32 ] || fail "handoff_invalid"
fi
if [ -n "$VERIFICATION_HOME" ]; then
  vh_checked="$(validate_verification_home)" || fail "verification_home_invalid"
  VERIFICATION_HOME="$vh_checked"
fi
[ -f "$ARCHIVE" ] || fail "archive_missing"
[ -d "$TARGET" ] || fail "target_missing"

# 最多等 20 秒，绝不强杀主程序，避免截断其保存用户配置的退出流程。
count=0
while kill -0 "$PID" 2>/dev/null; do
  count=$((count + 1))
  [ "$count" -le 40 ] || fail "main_app_exit_timeout"
  sleep 0.5
done

actual_sha256="$(/usr/bin/shasum -a 256 "$ARCHIVE" | /usr/bin/awk '{print $1}')"
[ "$actual_sha256" = "$EXPECTED_SHA256" ] || fail "archive_hash_mismatch"

# 在解压前检查 ZIP 条目。拒绝危险软链接、绝对/穿越路径与膨胀包；
# PyInstaller 自带的包内相对软链接在解压后还会再验证绝不逃出候选 app。
#
# v1.4.59（S6）：这一段原先跑在 /usr/bin/python3 上，而 **/usr/bin/python3 不是系统自带** ——
# 它是 Command Line Tools 的桩：没装开发者工具的机器上文件在、一执行就失败（有的还弹
# 「未安装命令行开发者工具」的安装框）。后果是 OTA 在这类机器上必停 archive_preflight_failed，
# 而它看起来像"包有问题"，其实是一台没装 Xcode 工具的正常 Mac。助手是装机版唯一一条
# 自动更新通路，不该依赖开发者工具，所以换成 zipinfo（Info-ZIP，与 unzip 同一份
# root:wheel 系统件）读中央目录。本机对线上 1.4.57 资产逐字段实测，两版口径一致：
#   zipinfo 汇总行 → 561 files, 74069005 bytes uncompressed
#   python zipfile → 561 条, 合计 74069005 字节
# ⚠ LC_ALL=C 是必需的，不是风格：ditto 造的非 ASCII 条目名（例如旧顶层名
# 「实时股价-目标价版.app」）经 zipinfo 按代码页转换后会吐出**非法 UTF-8 字节**，
# macOS 的 awk 一碰到就整条中止（实测 towc: multibyte conversion failure），
# 于是预检必然失败、OTA 对这类包永远装不上。按字节处理才对：这里要认的字符
# （/ \ .. 与空分段）全是 ASCII，多字节序列里根本不会出现它们（UTF-8 的后续字节
# 一律 >=0x80），所以字节口径既不会误拒也不会漏判。
zip_stats="$(LC_ALL=C /usr/bin/zipinfo "$ARCHIVE" 2>/dev/null | LC_ALL=C /usr/bin/awk \
    -v maxf="$MAX_UNPACK_FILES" -v maxb="$MAX_UNPACK_BYTES" '
  NR <= 2 { next }                       # 前两行是 Archive: / Zip file size: 表头
  / bytes uncompressed,/ {               # 收尾汇总行：条目数与解压后总字节
      # 这行以数字开头（"561 files, 74069005 bytes uncompressed, ..."），
      # 所以按非数字切开后第 1 段就是条目数 —— 别按"前面有分隔符"的直觉写成 num[2]，
      # 那样取到的是总字节数，条目数会变成天文数字、每个正常包都被判成膨胀包。
      split($0, num, /[^0-9]+/)
      entries = num[1] + 0
      total = num[2] + 0
      next
  }
  {
      # 名字是"去掉前 8 个字段之后的整行"，不能直接用 $9 —— 条目名里可以有空格。
      name = $0
      for (i = 1; i <= 8; i++) sub(/^[^ \t]+[ \t]+/, "", name)
      path = name
      gsub(/\\/, "/", path)               # 反斜杠也当分隔符看（与原口径一致）
      n = split(path, parts, "/")
      if (substr(path, 1, 1) == "/") { bad = "archive_path_invalid" }
      for (j = 1; j <= n; j++) {
          if (parts[j] == "..") { if (!bad) bad = "archive_path_invalid" }
          # 空分段（例如 a//b）：ditto 会在临时目录里造出意料外的层级
          if (parts[j] == "" && j < n) { if (!bad) bad = "archive_path_invalid" }
      }
      # 软链接本身是合法的（PyInstaller 用相对链接承载 dylib），但链接名不许是绝对/穿越路径
      if (substr($1, 1, 1) == "l" && (substr(path, 1, 1) == "/" || index(path, "../") > 0)) {
          if (!bad) bad = "archive_symlink_invalid"
      }
      seen++
  }
  END {
      if (bad) { print "ERROR:" bad; exit 1 }
      if (seen == 0 || entries == 0 || entries > maxf) {
          print "ERROR:archive_file_count_invalid"; exit 1
      }
      if (total > maxb) { print "ERROR:archive_unpacked_size_invalid"; exit 1 }
      print total
  }
' )" || fail "archive_preflight_failed"
case "$zip_stats" in
  *[!0-9]*) fail "archive_preflight_failed" ;;
esac

# 预留“解压后的新包 + 旧包回退 + 固定缓冲”空间，避免移动到一半才因磁盘满中断。
free_kb="$(/bin/df -Pk "$(dirname "$TARGET")" | /usr/bin/awk 'NR==2 {print $4}')"
target_kb="$(/usr/bin/du -sk "$TARGET" | /usr/bin/awk '{print $1}')"
case "$free_kb:$target_kb" in *[!0-9:]*|:) fail "disk_space_unknown" ;; esac
need_bytes=$((zip_stats + target_kb * 1024 + MIN_FREE_BYTES))
free_bytes=$((free_kb * 1024))
[ "$free_bytes" -ge "$need_bytes" ] || fail "insufficient_disk_space"

work="$(mktemp -d "$(dirname "$ARCHIVE")/unpack.XXXXXX")"
owner_record="${work}.owner"
printf '%s\n' "$$" > "$owner_record"
retire_unpack_work() {
  [ -d "$work" ] || return 0
  # Only this invocation's private staging directory is moved, never TARGET or rollback.
  recovery_root="${TMPDIR:-/tmp}/sf_update_unpack_recovery"
  [ ! -L "$recovery_root" ] || return 0
  /bin/mkdir -p "$recovery_root" 2>/dev/null || return 0
  /bin/chmod 700 "$recovery_root" 2>/dev/null || return 0
  recovery_slot="$(mktemp -d "$recovery_root/unpack.XXXXXX")" || return 0
  /usr/bin/find "$work" -depth -type d -name '*.app' \
    -exec /bin/mv '{}' '{}.stale' \; 2>/dev/null || true
  /bin/mv "$work" "$recovery_slot/work" 2>/dev/null || true
  /bin/mv "$owner_record" "$recovery_slot/owner" 2>/dev/null || true
}
trap retire_unpack_work EXIT
# macOS 没有 timeout；用 Perl alarm 为 ditto 提供硬超时。
/usr/bin/perl -e 'alarm shift; exec @ARGV' "$EXTRACT_TIMEOUT" /usr/bin/ditto -x -k "$ARCHIVE" "$work" || fail "archive_extract_failed"

# 顶层目录名跟着产品名走。v1.4.55 改名（2f46b50）把 bundle id、DMG、更新资产都换了，
# 唯独这一行漏了 —— 实测拿线上 1.4.57 的资产跑装机版助手，解包后立刻
# archive_layout_invalid 并回滚，也就是改名之后自动更新一次都没成功过。
# AGENTS §3 说"五处必须两代都认"，这里是第六处；名字的唯一来源是
# sf_constants.APP_DISPLAY_NAME，漂移由 tools/verify_install_paths.py 拦。
candidate=""
for _name in "Skyrocket.app" "实时股价-目标价版.app"; do
  if [ -d "$work/$_name" ]; then
    candidate="$work/$_name"
    break
  fi
done
[ -n "$candidate" ] || fail "archive_layout_invalid"
[ -d "$candidate" ] || fail "archive_layout_invalid"
[ "$(/bin/ls -A "$work" | /usr/bin/wc -l | /usr/bin/tr -d ' ')" = "1" ] || fail "archive_layout_invalid"
# 允许包内 dylib 的相对软链接，但任何链接都不得指向 app 包外。
# v1.4.59（S6）：同上一段，原来跑在 /usr/bin/python3（Command Line Tools 桩）上，
# 换成系统自带的 perl —— 本脚本为了给 ditto 做硬超时本来就依赖 perl，没有新增依赖。
# 与 Python 版同口径的地方实测过：包内的悬空相对链接（目标解析后仍在 app 里）两边都
# 放行；Cwd::realpath 取不到值时判不合格。不变量只有一条 —— 任何链接都不许逃出 app 包，
# 由 tests/test_update_helper.py 用"相对/绝对/逃出包"三种链接各跑一次真助手来钉。
if ! /usr/bin/perl -MCwd -e '
    use strict; use warnings;
    my $root = Cwd::realpath($ARGV[0]);
    defined $root or exit 1;
    my $bad = 0;
    require File::Find;
    File::Find::find(sub {
        my $p = $File::Find::name;
        return unless -l $p;
        my $r = Cwd::realpath($p);
        if (!defined $r) { $bad = 1; return }
        if ($r ne $root && index($r, $root . "/") != 0) { $bad = 1 }
    }, $ARGV[0]);
    exit($bad ? 1 : 0);
' "$candidate" 2>/dev/null
then
  fail "archive_symlink_invalid"
fi

bundle_id="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$candidate/Contents/Info.plist" 2>/dev/null || true)"
candidate_version="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$candidate/Contents/Info.plist" 2>/dev/null || true)"
# v1.4.55 起本程序改名，bundle id 一并换成 com.skyrocket.app；旧值继续接受，
# 因为「已装旧版 + 新下载的包」这种组合在过渡期里会真实出现（回滚路径也一样）。
case "$bundle_id" in
  com.skyrocket.app|com.stockfloat.targetprice) ;;
  *) fail "bundle_identifier_invalid" ;;
esac
[ "$candidate_version" = "$EXPECTED_VERSION" ] || fail "bundle_version_invalid"
/usr/bin/codesign --verify --deep --strict "$candidate" >/dev/null 2>&1 || fail "new_app_signature_invalid"

# 证书上线后必须同时配置 Team ID 与证书 SHA-1，并精确锚定身份。
if [ -n "$EXPECTED_TEAM_ID$EXPECTED_CERT_SHA1" ]; then
  [ -n "$EXPECTED_TEAM_ID" ] && [ -n "$EXPECTED_CERT_SHA1" ] || fail "signing_anchor_configuration_invalid"
  sign_info="$(/usr/bin/codesign -dvv "$candidate" 2>&1 || true)"
  printf '%s\n' "$sign_info" | /usr/bin/grep -E "Identifier=(com\.skyrocket\.app|com\.stockfloat\.targetprice)$" >/dev/null || fail "signing_identity_invalid"
  printf '%s\n' "$sign_info" | /usr/bin/grep -F "TeamIdentifier=$EXPECTED_TEAM_ID" >/dev/null || fail "signing_team_invalid"
  /usr/bin/codesign -d --extract-certificates "$candidate" "$work/cert" >/dev/null 2>&1 || fail "signing_certificate_missing"
  cert_sha1="$(/usr/bin/openssl x509 -in "$work/cert0" -noout -fingerprint -sha1 2>/dev/null | /usr/bin/awk -F= '{print $2}' | /usr/bin/tr -d ':')"
  [ "$cert_sha1" = "$EXPECTED_CERT_SHA1" ] || fail "signing_certificate_invalid"
fi

parent="$(dirname "$TARGET")"
base="$(basename "$TARGET")"
rollback="$parent/.${base}.rollback-$(date +%s)"

# 同卷 rename 保证替换过程可回退；任何一步失败都立即把旧版放回原位。
if ! /bin/mv "$TARGET" "$rollback"; then
  fail "backup_old_app_failed"
fi
if ! /bin/mv "$candidate" "$TARGET"; then
  rollback_or_fail "install_new_app_failed"
fi

if ! /usr/bin/codesign --verify --deep --strict "$TARGET" >/dev/null 2>&1; then
  rollback_or_fail "installed_app_signature_invalid"
fi

if ! launch_app "$TARGET" >/dev/null 2>&1; then
  rollback_or_fail "restart_new_app_failed"
fi
# open 返回只代表系统接受了启动请求；必须确认新进程出现并持续存活，避免“安装完成但马上崩溃”被误报为成功。
# v1.4.59（S6 顺带抓到的一处）：这一行原先把"新版的可执行文件路径"写死成**改名前**的旧名
# （Contents/MacOS/ 拼旧 APP 名），而 v1.4.55 改名后可执行文件叫 Skyrocket —— 也就是说
# 解包与替换都成功之后，存活复核
# pgrep 永远找不到进程，助手会把**装好了的新版**回滚掉并判 restart_new_app_failed。
# 它一直没暴露，是因为更早的 archive_layout_invalid 先把流程挡住了（同一批改名漏改，
# 前者先命中）；1.4.58 修掉那一处之后，这一处就是下一张多米诺。
# 修法不再列名字清单（清单正是漂移的来源），直接读 Info.plist 的 CFBundleExecutable ——
# 那是打包时唯一决定这个名字的字段。读不到就判失败并回滚：连身份都不明的事不放行。
new_exec_name="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleExecutable' \
  "$TARGET/Contents/Info.plist" 2>/dev/null || true)"
[ -n "$new_exec_name" ] || rollback_or_fail "restart_new_app_failed"
new_exec="$TARGET/Contents/MacOS/$new_exec_name"
started=0
count=0
while [ "$count" -lt 20 ]; do
  if /usr/bin/pgrep -f "$new_exec" >/dev/null 2>&1; then
    started=1
    break
  fi
  count=$((count + 1))
  sleep 0.5
done
[ "$started" -eq 1 ] || rollback_or_fail "restart_new_app_failed"
# 首次出现后走握手确认；旧调用仍观察十秒存活，防止启动后立即退出被误报成功。
  ack_ticks=60
  if [ -n "$VERIFICATION_HOME" ]; then
    case "${SF_HANDOFF_ACK_TICKS:-}" in
      ""|*[!0-9]*) : ;;
      *) if [ "${SF_HANDOFF_ACK_TICKS:-0}" -ge 1 ] && [ "${SF_HANDOFF_ACK_TICKS:-0}" -le 600 ]; then
           ack_ticks="$SF_HANDOFF_ACK_TICKS"
         fi ;;
    esac
  fi
if [ -n "$HANDOFF" ]; then
    handoff_ack="$(dirname "$RESULT")/handoff-$HANDOFF.ack.json"
    count=0
  confirmed=0
  while [ "$count" -lt "$ack_ticks" ]; do
    /usr/bin/pgrep -f "$new_exec" >/dev/null 2>&1 || rollback_or_fail "restart_new_app_failed"
    if [ -f "$handoff_ack" ] \
       && LC_ALL=C /usr/bin/grep -F "\"handoff_id\":\"$HANDOFF\"" "$handoff_ack" >/dev/null 2>&1 \
       && LC_ALL=C /usr/bin/grep -F "\"version\":\"$EXPECTED_VERSION\"" "$handoff_ack" >/dev/null 2>&1 \
       && LC_ALL=C /usr/bin/grep -F '"bundle_id":"com.skyrocket.app"' "$handoff_ack" >/dev/null 2>&1
    then
      confirmed=1
      break
    fi
    count=$((count + 1))
    sleep 0.5
  done
  [ "$confirmed" -eq 1 ] || rollback_or_fail "startup_confirmation_timeout"
else
  # Legacy callers do not create a handoff acknowledgement. Preserve the
  # previously released 10-second post-launch survival check for them.
  count=0
  while [ "$count" -lt 20 ]; do
    /usr/bin/pgrep -f "$new_exec" >/dev/null 2>&1 || rollback_or_fail "restart_new_app_failed"
    count=$((count + 1))
    sleep 0.5
  done
fi
write_result "installed" "ok"

# ---- 回退包只保留最近一份（v1.4.59 起，S6 第一项）----
# 原来这条路径上每一份回退包都永久留在 /Applications 里（名字以 . 开头，Finder 看不见、
# 用户清不掉），而一次约 100MB —— 更新十次就是 1GB 藏在「应用程序」下面。留最新一份是有意的
# （那是"上一次更新之前的版本"，出问题还能人工恢复），更早的那份已经隔了两个版本，
# 既不是任何回退路线的落点（AGENTS §5：应用级回退走 build/_rollback、桌面上一版 DMG、
# OTA 仓库里的上一版 zip），又只会把磁盘填满 —— 本机实测用户盘只剩几十 GB 时，
# 空间不足本身就会让下一次更新停在 insufficient_disk_space。
# 一律 mv 不删文件：送系统临时目录，由 macOS 自己回收（与 build_release.sh 处理 _prev 同法）。
# 目的地走 ${TMPDIR:-/tmp}：既保持"同卷 rename、瞬间完成"，也让这条正向路线能被
# tests/test_update_helper.py 把 TMPDIR 指到夹具目录后真跑一遍 —— 判据落在
# "谁还在 /Applications、谁被挪走了"这件可观测的事上，而不是读脚本措辞。
# 这些目录名以 .rollback-<epoch> 结尾、不以 .app 结尾，所以落到临时目录也不会被
# LaunchServices 当成应用重新拉起（v1.4.5 那枚 SIGSEGV 的成因正是可启动副本躺在临时目录）。
prune_dir="${TMPDIR:-/tmp}/sf_update_rollbacks"
# BEGIN prune_rollbacks
# 这一段做成函数、并留这对标记，是为了能被**单独跑起来验**：正向路线要真把新版
# 拉起来才走得到，而测试夹具不该在用户屏幕上启动 GUI（smoke_frozen_app 用 offscreen
# 就是为躲这件事）。测试用 tests/test_update_helper.py 把函数体抽出来在 /bin/sh 里
# 真跑一遍，判据是目录树本身（谁还在 /Applications、谁被挪走了），不是读脚本措辞。
# 改这个函数时请同步那条用例，别把标记行删掉 —— 标记没了，用例当场红。
prune_rollbacks() {
  # v1.4.61：安装目录这一层**自己从 TARGET 现算**，不借外层的 parent/base。
  # 那枚真缺陷就是 parent 被 write_result 覆盖掉，而这一段扫不到东西时既不报错也不
  # 留痕（判据是目录树，扫错目录=什么都不做=零输出）—— 能自己算的变量绝不依赖别人算过。
  rb_root="$(dirname "$TARGET")"
  rb_base="$(basename "$TARGET")"
  tab="$(printf '\t')"
  # 候选 = 「<epoch><TAB><路径>」每行一条。改名（v1.4.55）之前装的回退包叫
  # .实时股价-目标价版.app.rollback-<epoch>，只按当前包名扫永远扫不到它们：实测一台机器上
  # 躺着 30 份、约 5.2GB。所以名字不同的隐藏回退包也进候选，但**必须读得到 Info.plist 里
  # 的包标识且是本程序两代之一**才算（与上面安装前的标识校验同一份口径，不另造名字清单）；
  # 读不到、标识是别家的，一律不碰。epoch 必须是纯数字（date +%s 写出来的就是），否则不认。
  cands=""
  for cand in "$rb_root"/.*.rollback-*; do
    [ -d "$cand" ] || continue
    cand_name="${cand##*/}"
    cand_epoch="${cand_name##*.rollback-}"
    case "$cand_epoch" in
      ''|*[!0-9]*) continue ;;
    esac
    if [ "$cand_name" != ".$rb_base.rollback-$cand_epoch" ]; then
      cand_id="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$cand/Contents/Info.plist" 2>/dev/null || true)"
      case "$cand_id" in
        com.skyrocket.app|com.stockfloat.targetprice) ;;
        *) continue ;;
      esac
    fi
    cands="$cands$cand_epoch$tab$cand
"
  done
  [ -n "$cands" ] || return 0
  # 不同包名的字典序不再是时间序，所以按名字里的 epoch 数值排；除最新一份之外全部挪走。
  ordered="$(printf '%s' "$cands" | LC_ALL=C /usr/bin/sort -n -t "$tab" -k1,1)"
  total="$(printf '%s\n' "$ordered" | /usr/bin/grep -c . || true)"
  if [ "${total:-0}" -gt 1 ]; then
    /bin/mkdir -p "$prune_dir" 2>/dev/null || true
    printf '%s\n' "$ordered" | /usr/bin/head -n "$((total - 1))" | while IFS= read -r line; do
      stale="${line#*"$tab"}"
      [ -n "$stale" ] || continue
      /bin/mv "$stale" "$prune_dir"/ 2>/dev/null || true
    done
  fi
}
# END prune_rollbacks
prune_rollbacks || true
exit 0
