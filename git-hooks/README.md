---
AIGC:
    Label: "1"
    ContentProducer: 001191440300708461136T1XGW3
    ProduceID: 8e2d67b8fb28f1b31d684e70e95208d3_13267e6da12411f1a413525400287e28
    ReservedCode1: nTlPFSY2SZv3WevIr+g7y/FgW44MbiGod9S73bcRJxY4UVkiFYsI3h0T4OzKKYvuJbX65+qvwKO2IulxGAgaVjw0fJzq/4lpKtVtxrZiy5YTtghHX45SV/1JsiRqfk1WHc5weD1ysINwSUrKNwIplKl7B4aMsbYyUqpbCmRJC2Ik9fz4LBADm6h8vic=
    ContentPropagator: 001191440300708461136T1XGW3
    PropagateID: 8e2d67b8fb28f1b31d684e70e95208d3_13267e6da12411f1a413525400287e28
    ReservedCode2: nTlPFSY2SZv3WevIr+g7y/FgW44MbiGod9S73bcRJxY4UVkiFYsI3h0T4OzKKYvuJbX65+qvwKO2IulxGAgaVjw0fJzq/4lpKtVtxrZiy5YTtghHX45SV/1JsiRqfk1WHc5weD1ysINwSUrKNwIplKl7B4aMsbYyUqpbCmRJC2Ik9fz4LBADm6h8vic=
---

# 本地强制检查关卡（v1.2.28 第3项）

## 发版强制关卡：./run_tests.sh

工程根目录的 `./run_tests.sh` 是**发版前必须运行**的强制测试关卡：

- 一键跑通 `tests/` 下全部单元/回归测试，实际数量和条件跳过以本轮日志为准。
- 已含 `set -euo pipefail`，任一用例失败即**非零退出码**，可阻断打包/发布流程。
- 使用工程 `.venv`（Python 3.14.7 + PyQt6），不要用系统 Python 代替。
- 完整合并流程见 [`docs/BRANCH_MERGE_WORKFLOW.md`](../docs/BRANCH_MERGE_WORKFLOW.md)；
  回归通过不自动授权打包、安装或发布。

## Git pre-commit 钩子

每棵工作树均须核验实际路径、内容和可执行权限，不依据本文宣称“已安装”。路径使用
`git rev-parse --path-format=absolute --git-path hooks/pre-commit` 解析，不能写死 `.git/hooks`。

| 文件 | 作用 |
| --- | --- |
| `git-hooks/pre-commit` | 提交前自动跑 py_compile + ./run_tests.sh，任一失败阻止提交并给出清晰提示 |
| `git-hooks/install-hooks.sh` | 安装/核验实际 hook 路径，支持 linked worktree 与 `core.hooksPath` |

### 重装命令（钩子丢失或换机克隆后）

```bash
./git-hooks/install-hooks.sh
```

之后每次 `git commit` 会先自动执行：

1. `py_compile` 全量语法检查（排除 backups/.git）；
2. `./run_tests.sh` 全量测试；
3. 任一失败 → 非零退出，**阻止提交**并打印失败原因。

同内容的 hook 可重复核验并启用；不同内容、符号链接或非普通文件一律拒绝覆盖，先审查、
获准备份并协调处理，不提供强制覆盖开关。默认 hooks 可由多棵 linked worktree 共享，
自定义 hooks 路径也可能是共享目录，修改前确认其他会话不受影响。
该命令不修改 Git 配置，不绕过已有 hook，也不主动执行完整回归。最终是否实际执行以正常提交日志为准。
遇到 `GIT_DIR`、`GIT_WORK_TREE`、`GIT_COMMON_DIR` 或 `GIT_INDEX_FILE` 路由环境会停止；
请在普通终端核对仓库后执行，不在另一次 Git 操作或 hook 内安装。
*（内容由AI生成，仅供参考）*
