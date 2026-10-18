#!/usr/bin/env bash
# 安装到 Git 实际使用的路径，兼容 linked worktree 和 core.hooksPath。
set -euo pipefail
if [ "$#" -ne 0 ]; then
  echo "!! 不接受参数；用法：./git-hooks/install-hooks.sh" >&2
  exit 2
fi
if [ -n "${GIT_DIR:-}${GIT_WORK_TREE:-}${GIT_COMMON_DIR:-}${GIT_INDEX_FILE:-}" ]; then
  echo "!! 检测到 Git 路由环境；请在普通终端中核对仓库后安装，不从其他 Git 操作中安装。" >&2
  exit 1
fi
cd "$(dirname "$0")/.."
ROOT="$(pwd -P)"
if ! GIT_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)"; then
  echo "!! 当前目录不是 Git 工作树；请先核对仓库路径，不自动初始化。" >&2
  exit 1
fi
if [ "$GIT_ROOT" != "$ROOT" ]; then
  echo "!! Git 顶层与脚本目录不一致；请检查 GIT_* 环境，不修改其他仓库。" >&2
  exit 1
fi
SOURCE="$ROOT/git-hooks/pre-commit"
TARGET="$(git rev-parse --path-format=absolute --git-path hooks/pre-commit)"
if [ ! -f "$SOURCE" ]; then
  echo "!! 缺少仓库内 pre-commit 源文件。" >&2
  exit 1
fi
if [ -L "$TARGET" ] || { [ -e "$TARGET" ] && [ ! -f "$TARGET" ]; }; then
  echo "!! 实际 hook 路径是符号链接或非普通文件，拒绝覆盖：$TARGET" >&2
  exit 1
fi
if [ -f "$TARGET" ]; then
  if ! cmp -s "$SOURCE" "$TARGET"; then
    echo "!! 已有不同内容的 hook，拒绝覆盖：$TARGET" >&2
    echo "!! 请先审查、获准备份并处理已有 hook，再重新安装。" >&2
    exit 1
  fi
else
  mkdir -p "$(dirname "$TARGET")"
  cp "$SOURCE" "$TARGET"
fi
chmod +x "$TARGET"
echo "== pre-commit 钩子已核验并启用：$TARGET =="
echo "== 后续每次 git commit 将自动运行 py_compile + ./run_tests.sh，任一失败阻止提交 =="
