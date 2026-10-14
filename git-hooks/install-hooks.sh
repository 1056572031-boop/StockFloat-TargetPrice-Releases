#!/usr/bin/env bash
# 安装 git pre-commit 钩子（需先在 git 仓库内执行；未 git init 时提示先初始化）
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -d .git ]; then
  echo "!! 当前目录不是 git 仓库，无法启用 pre-commit 钩子。"
  echo "!! 请先执行: git init"
  echo "!! 之后再次运行: ./git-hooks/install-hooks.sh"
  exit 1
fi
mkdir -p .git/hooks
cp git-hooks/pre-commit .git/hooks/pre-commit
chmod +x .git/hooks/pre-commit
echo "== pre-commit 钩子已安装到 .git/hooks/pre-commit =="
echo "== 后续每次 git commit 将自动运行 py_compile + ./run_tests.sh，任一失败阻止提交 =="
