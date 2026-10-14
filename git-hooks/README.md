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

- 一键跑通 `tests/` 下全部单元/回归测试（`unittest discover`，423 项）。
- 已含 `set -euo pipefail`，任一用例失败即**非零退出码**，可阻断打包/发布流程。
- 发版流程：改码 → `python3 -m py_compile`（语法） → `./run_tests.sh`（全量） → 通过后打包发布。

## Git pre-commit 钩子

项目已完成 `git init`（当前为 git 仓库），pre-commit 钩子**已通过 `install-hooks.sh` 安装并生效**（位于 `.git/hooks/pre-commit`）。

| 文件 | 作用 |
| --- | --- |
| `git-hooks/pre-commit` | 提交前自动跑 py_compile + ./run_tests.sh，任一失败阻止提交并给出清晰提示 |
| `git-hooks/install-hooks.sh` | 一键安装钩子到 `.git/hooks/pre-commit` |

### 重装命令（钩子丢失或换机克隆后）

```bash
cd "…/StockFloat"
./git-hooks/install-hooks.sh     # 复制并启用 pre-commit 钩子
```

之后每次 `git commit` 会先自动执行：

1. `py_compile` 全量语法检查（排除 backups/.git）；
2. `./run_tests.sh` 全量测试；
3. 任一失败 → 非零退出，**阻止提交**并打印失败原因。

也可手动执行 `cp git-hooks/pre-commit .git/hooks/pre-commit && chmod +x .git/hooks/pre-commit` 等效重装。
*（内容由AI生成，仅供参考）*
