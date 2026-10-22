# Skyrocket 分支合入主线标准流程

适用：本仓库功能、修复、重构、测试及工具分支合入开发主线。制定日期：2026-10-18。
这是操作规约和验收模板，不是自动执行器，也不是对现存分支的合并授权。
修订日期：2026-10-19；补齐索引一致性、hooks 实际路径、环境短路、工作树增量、发行输入冻结和推送地址验收。

## 1. 不可混淆的边界

- 当前开发主线是 `feat/target-price`，不是按名称猜测的 `main`、`master` 或公开仓的
  `public-source`。每轮仍须核对目标仓库、分支、工作树及双方完整提交 SHA。
- 分支开发、审查修复、合并授权、主线验收、远端推送、打包安装和正式发布分别记账。
  “可以开发/修复”不等于“可以合并”；“合并到主线”不自动授权推送、安装或发布。
- 只有用户明确同意本轮合并范围后才移动主线。获准后若双方提交或合并范围变化，重新预检；
  涉及新增功能、风险或未批准提交时，重新取得用户同意。`AGENTS.md` 的持续执行授权不替代这一条：
  它满足其他流程的授权确认，合并主线仍须用户明确同意本轮范围。
- 审批绑定固定提交，不追随移动中的来源分支。批准后来源分支继续开发，只要本次仍合入获批
  SHA，就不自动扩大范围；主线变化则必须重新集成、审查和验收，不沿用旧预演。
- 未完成的真实场景保持未验证。源码合并通过不表示冻结包、安装、摄像头权限、OTA、
  OTA rollback、跨机或最终启动通过。发布另走 `RELEASE_PREPARATION_WORKFLOW.md`。

## 2. 状态与出口

| 状态 | 必需证据 | 下一步 |
| --- | --- | --- |
| `preparing` | 主线/来源 SHA、范围、脏文件和未验证项清单 | 分支审查修复 |
| `review_ready` | 审查问题闭环、定向测试、分支完整回归 | 等待用户合并同意 |
| `approved` | 用户同意及其对应的双方 SHA/范围 | 预检后实际合并 |
| `integrated_pending_validation` | 实际合并内容或提交已产生，尚未完成验收 | 主线验收或停止修复 |
| `source_merge_accepted` | 实际合并结果、完整关卡、同源指纹及专项验收 | 归档；推送/发布另行授权 |
| `blocked` | 失败命令、日志、原因、未完成项和恢复点 | 解决后从受影响阶段重做 |

任何“无文本冲突”“预演通过”“退出码 0”都不能单独把状态推进到 `source_merge_accepted`。
该状态只表示本次固定提交的源码验收，不表示之后仍在变化的主线或来源分支都通过。

## 3. M0：登记和保护现场

先只读检查，输出保存在仓库外的本轮独立证据目录。不要把原始日志写进会被构建清空的目录，
不要把视频、诊断包、个人数据或候选包加入提交。保留原始失败日志，修复后另存新一轮结果。

```bash
pwd
git rev-parse --show-toplevel
git remote -v
git status --short --branch
git diff --stat
git diff --cached --stat
git ls-files --others --exclude-standard
git worktree list --porcelain
git branch -vv
git config --show-origin --get core.hooksPath
git rev-parse --is-shallow-repository
git config --get core.sparseCheckout
git status --porcelain=v1 --untracked-files=all
```

配置项未配置时 `git config --get` 可返回 1，不误判成回归失败；浅克隆、稀疏检出、未初始化的
子模块或缺失 Git LFS 对象先补齐本轮需要的历史/文件。多共同祖先或无共同祖先时停止普通路径，
单独确定集成方案，不使用 `--allow-unrelated-histories` 强行合并，不任取一个 merge base。
远端 URL、路径或日志可能带私密信息，公开前脱敏，不能直接作为公开交付物。

1. 登记来源分支、目标主线、共同祖先、双方完整 SHA、当前版本、负责人和允许改动文件。
2. 检查暂存、未暂存和未跟踪文件。已有修改默认属于用户或其他会话；不得覆盖、自动提交、
   自动 stash 或用 `reset --hard`、`checkout --` 清场。不能隔离时停止实际合并。
   还要逐个读相关工作树的 `git status`：来源指针已是主线祖先，不代表它的未提交内容已合入；
   反过来，脏文件也可能逐字相同或已由主线更新实现替代。按相同、已替代、新增、需语义复核逐文件列明，
   不用旧同步实现覆盖新的异步实现；同名测试文件中的不同有效判据也要保留。
3. 主线工作树和来源分支在实际合并前应干净。若存在不属本轮的工作，先保留现场，选择已批准的
   隔离方案；不要为图方便切换正在被其他工作树占用的分支，也不要强行解除占用。
   若来源分支继续有并行工作，可从批准 SHA 在独立工作树验收，不要求把对方未提交内容带入。
   `git status` 干净不包含 ignored 文件的安全证明：本地环境、密钥等还要与将写入的跟踪路径核对。
4. `.venv` 必须由本机工程环境提供，不属于源码交付。使用或建立符号链接前检查：

```bash
git ls-files --stage -- .venv '.venv/*'
git check-ignore .venv .venv/
./.venv/bin/python --version
./.venv/bin/python -c 'import PyQt6'
```

要求 Python 3.14.7，PyQt6 可导入；跟踪列表中不得出现 `.venv` 本体或内部文件。
在 M1 固定来源 SHA 后执行 `git ls-tree -r "$SOURCE_SHA" -- .venv`，检查目录、文件及模式为
`120000` 的链接。不能以主线当前未跟踪或被 ignore 为充分证据；来源跟踪路径若与本地 ignored
环境/数据相撞，停止并先隔离，不让 Git 在合并时替换它们。
不要重建固定签名密钥、改真实用户数据目录或启动正式应用做无关检查。

## 4. M1：固定来源和审查范围

下面是逐条执行的 Bash 模板，不是一键脚本。`SOURCE_REF` 须替换为已核对的完整分支引用；
变量解析失败立即停止，不继续执行后续命令。SHA 一旦记录，不用会移动的分支名替代。
需要获知远端现状时先对已核对的远端 fetch，再重新解析和登记；不要直接
`git pull` 将未审查内容带入。离线时明确“远端未刷新”。

```bash
MAINLINE_REF='refs/heads/feat/target-price'
SOURCE_REF='refs/heads/codex/REPLACE_WITH_APPROVED_BRANCH'
MAINLINE_SHA="$(git rev-parse --verify "${MAINLINE_REF}^{commit}")"
SOURCE_SHA="$(git rev-parse --verify "${SOURCE_REF}^{commit}")"
git merge-base --all "$MAINLINE_SHA" "$SOURCE_SHA"
```

确认只有一个共同祖先后，记录其完整 SHA 为 `BASE_SHA`；检查必需变量后再继续。下文所有
`test` 或 Git 检查命令非零都表示停止，不是“看到了输出就继续”。

```bash
: "${MAINLINE_SHA:?尚未固定主线提交}"
: "${SOURCE_SHA:?尚未固定来源提交}"
: "${BASE_SHA:?尚未记录唯一共同祖先}"
git log --oneline "${MAINLINE_SHA}..${SOURCE_SHA}"
git diff --stat "$BASE_SHA" "$SOURCE_SHA"
git diff --name-status "$BASE_SHA" "$SOURCE_SHA"
git diff --check "$BASE_SHA" "$SOURCE_SHA"
git diff --summary "$BASE_SHA" "$SOURCE_SHA"
git diff --stat "$BASE_SHA" "$MAINLINE_SHA"
git ls-tree -r "$SOURCE_SHA" -- .venv
```

- 逐项确认来源独有提交；不带入并行功能、历史试验、临时证据或版本回退。
- 同时审查主线自共同祖先以来的变化，不只看来源差异；来源与主线都修改的协议需要兼容验证。
- 审查行为、异常路径、线程、持久化、身份、依赖、签名、安装及更新兼容性。移动代码也要检查
  回调、导入、词法作用域和诊断可达性；不能只看移动后的行数。
- 有持久化改动时按“取得什么、如何解析、保存什么、新进程读回什么、用户看到什么”验收。
- 定向回归覆盖新行为与失败路径；对关卡、分析器等修改增加可拒绝错误的负向对照。
- 新增惰性 import 核对 `StockFloat.spec` 的 `hiddenimports`；依赖变化同步锁定文件。
- 检查权限/软链接变化、两代应用身份、shell 中文变量边界、公开 CI 仅触发 `public-source`。
- 静态、覆盖率、书写纪律等基线不因合并冲突取更宽值，不为过门禁新增豁免。
- 本轮还授权发布时，先做发布流程 P0 的协议、锁、桌面阶段边界和既有公开历史只读预检，
  不等 OTA 已切换后才发现历史数据分类或默认分支差异。单独的源码合并仍不自动授权发布。

在来源工作树运行定向测试及标准完整关卡，记录命令、退出码、失败/跳过项和证据路径。
完整关卡使用下方带日志的写法执行，不为了整理记录重复运行同一份源码的同一关卡。

正式应用正在运行时遵守守卫，提示用户正常退出后重跑；不杀用户进程，不绕过守卫。
覆盖率凭据复核使用原运行入口 `.venv/bin/python3`，不把同实路径的 `.venv/bin/python` 别名视为相同执行身份。
不得以 `SF_SKIP_COVERAGE=1`、`SF_RANDOM_ORDER=1`、公开回归模式或手工 `gate_stamp.py write`
替代完整关卡。特别检查并清除遗留的 `SF_GUARD_PROBE`：只要非空（即使是字符串 `0`），
`run_tests.sh` 都会在守卫后退出 0，根本没有跑完整回归。登记 `SF_CHANGE_BASE`、`COVERAGE_FILE`
等质量环境；不继承无法解释的测试选择、覆盖率或会话配置。分支绿戳不能复制到主线使用。

每轮命令日志保留原始退出码，不能用 `command | tee log` 的最后一项退出码冒充关卡退出码。
以下示例中的证据目录必须事先确认在仓库外、属于本轮当前阶段且日志文件不存在；分支验收、
主线验收、失败后重试各用新子目录。提交 hook 输出也按同样方式记录，不覆盖失败日志。
不因日志丢失凭记忆补填时间或退出码。

```bash
: "${EVIDENCE_DIR:?请先指定本轮独立证据目录}"
test -d "$EVIDENCE_DIR"
test ! -e "$EVIDENCE_DIR/full-gate.log"
if QT_QPA_PLATFORM=offscreen ./run_tests.sh >"$EVIDENCE_DIR/full-gate.log" 2>&1; then
  GATE_RC=0
else
  GATE_RC=$?
fi
printf '%s\n' "$GATE_RC"
test "$GATE_RC" -eq 0
./.venv/bin/python tools/gate_stamp.py check
```

## 5. M2：预演和合并决策

1. 核对实际主线仍等于 M1 记录的 SHA，来源仍使用批准 SHA。主线变化重新生成差异，审查旧结论是否仍适用。
2. 支持此模式的 Git 可执行 `git merge-tree --write-tree "$MAINLINE_SHA" "$SOURCE_SHA"`，
   保留输出及退出码。0 表示无文本冲突，1 表示冲突，其他错误停止；不能把错误输出首行当作成功 tree。
   该模式会写 Git 对象，但不移动主线/索引；有冲突按冲突处理，不支持时记录限制，不写成通过。
3. 无论预演是否通过，都审查双方同时修改的契约及语义冲突。版本、发布说明、README、安装指南、
   `release_checks.txt`、诊断声明和质量台账按当前真实状态协调，不能整份取 ours/theirs。
4. 合并前摘要必须包括：双方 SHA、纳入/排除范围、审查和测试结果、冲突处理方案、遗留未验证项、
   风险、恢复点，以及是否包含推送/发布。用户同意记录可引用实际聊天，不伪造审批或时间。

默认使用普通双亲合并保留审查来源。快进、squash、cherry-pick 或 rebase 如确有必要，应提前说明
溯源和回退影响并得到同意；不在合并时临时改策略，不改写已共享历史。

## 6. M3：实际合并与冲突收口

在已核对且干净的主线工作树执行；先记录合并前主线 SHA 作为恢复参考。
通过 `git rev-parse --path-format=absolute --git-path hooks/pre-commit` 解析实际 hook 文件，
核对其为可执行普通文件、内容与受审版本相同。使用 `./git-hooks/install-hooks.sh` 安装/复核；
该入口支持 linked worktree 和 `core.hooksPath`，已有不同 hook 或链接时拒绝覆盖，先审查和保留
原内容再处理。默认 hooks 在 linked worktree 间共享，修改前必须协调其他工作树，不能只看本轮。
安装应在普通终端中执行；入口遇到非空 `GIT_DIR`、`GIT_WORK_TREE`、`GIT_COMMON_DIR` 或
`GIT_INDEX_FILE` 会拒绝操作，不从正在运行的提交/合并 hook 内安装。

同一主线工作树从预检到验收交接只允许一个写入者；其他会话使用自己的分支/工作树。
开始前和提交后再次检查 HEAD、索引、跟踪文件和未跟踪文件；这是协作约定和检查，不是系统级锁。
Git 自带 index.lock 只保护单次操作，不能防止测试期间另一会话写文件。不能保证独占则停止。

```bash
: "${MAINLINE_REF:?先完成 M1}"
: "${MAINLINE_SHA:?先完成 M1}"
: "${SOURCE_SHA:?先完成 M1}"
test "$(git symbolic-ref HEAD)" = "$MAINLINE_REF"
test "$(git rev-parse HEAD)" = "$MAINLINE_SHA"
test -z "$(git status --porcelain=v1 --untracked-files=all)"
git merge --no-ff --no-commit --no-autostash "$SOURCE_SHA"
```

`--no-commit` 用于先检查真实合并内容；合并命令非零可能表示冲突，先读取状态，不自动重试或提交。
已经合入、来源是祖先等无新增变更情形，核实后记录“无需再次合并”，不要伪造新合并提交。
禁止自动 stash，防止合并后自动恢复隐藏修改。遇到已存在的 merge/rebase/cherry-pick/revert
操作先停止并查清归属，不在旧操作中继续本轮；记录“无需再次合并”也不自动补认历史验收。

冲突处理规则：

- 用 `git status` 和 `git diff --name-only --diff-filter=U` 列全冲突，每项记录双方意图、最终选择和测试。
- 只暂存本轮明确文件，使用 `git add -- <已检查的文件>`，禁止 `git add .` 或 `git add -A` 混入他人工作。
- 删除/改名须检查调用方、测试、spec、资源、脚本和文档引用；文本无冲突不等于功能兼容。
- 测试临时创建 Git 仓库时，子进程必须剔除所有 `GIT_*`，沿用 `tools/gate_stamp.py::git_env()`。
  不清洗正在执行真实合并所需的 hook 环境；清洗的是应独立运行的临时仓库子进程。
- 超出获批范围或无法解释的冲突立即停止。未提交合并需要取消时，先确认没有合并后新增的用户修改，
  经确认再 `git merge --abort`；不以破坏性 reset 代替恢复。

提交前检查真实合并树，不只检查来源分支：

```bash
git diff --name-only --diff-filter=U
git diff --cached --check
git diff --cached --stat
git diff --cached
git diff
git status --short
git diff --quiet --exit-code
REVIEWED_TREE="$(git write-tree)"
git rev-parse MERGE_HEAD
```

必须没有未解决冲突、未纳入的本轮修改、夹带的 `.venv`/证据/私密文件或不明权限变更。
`git diff --quiet --exit-code` 必须为 0，证明已跟踪工作文件与索引相同；`git write-tree`
遇未解决冲突必须失败。记录 `REVIEWED_TREE`，并确认 `MERGE_HEAD` 是唯一的批准来源 SHA。
未跟踪文件另行分类：本轮源码/依赖不得漏暂存，已知测试产物列入排除清单，未知项停止。
随后正常 `git commit` 生成合并提交，保留 hooks 完整输出和退出码；不得 `--no-verify` 或禁用 hooks。
若 hook 失败，状态仍是 `integrated_pending_validation` 或 `blocked`，修复并重新审查再提交。

## 7. M4：真实合并结果验收

实际结果必需有一次完整 `run_tests.sh` 通过证据。正常 `git commit` 的 pre-commit 若已在当前
真实合并内容上完成完整关卡，且提交前后无文件变化，可使用该轮日志，避免无意义重复；否则在
主线当前工作树重跑完整关卡。来源分支测试、merge-tree 预演和仅静态检查均不能替代。
复用还要求提交树与 `REVIEWED_TREE` 完全相同、工作文件与提交相同、完整关卡日志及退出码齐全，
且环境不是 probe/skip/public 模式。仅凭可用旧戳不够：它只覆盖规定的构建输入，并不绑定整个索引。

```bash
git show --no-patch --format=fuller HEAD
git rev-list --parents -n 1 HEAD
git merge-base --is-ancestor "$SOURCE_SHA" HEAD
test "$(git rev-parse 'HEAD^{tree}')" = "$REVIEWED_TREE"
git diff --quiet --exit-code HEAD
git status --short --branch
./.venv/bin/python tools/gate_stamp.py check
./.venv/bin/python tools/public_docs_check.py
```

1. 普通双亲合并检查第一父提交为合并前主线、第二父提交为批准的来源；记录最终合并 SHA。
2. 核对完整回归退出码、用例失败/错误/flaky/条件跳过和各覆盖率门禁，不只摘取“通过”字样。
3. 关卡指纹必须由当前主线工作树生成并验证；以 `gate_stamp.py` 当前实际输入名单为准，
   不能笼统假定“文档不影响指纹”（当前 README/INSTALL 等也在名单内）。变更后重新判定。
4. 按变更补充诊断、原生呈现或数据保护专项。涉及诊断声明时使用当前主线真实版本及有效范围：
   `./.venv/bin/python tools/diag_coverage_check.py --range "${DIAG_BASE_SHA}..HEAD" --version "$APP_VERSION"`。
   有效基线来自本轮版本/诊断规则并写明理由，不盲用过期版本范围，也不缩短受控覆盖区间。
5. 持久化测试隔离用户目录；如确需接触真实数据，事前说明并授权，记录前后读回和允许的变化。
   自动回归未覆盖的真实场景保留未验证，不冒充实机验收。
6. 任一必需项失败、索引/工作文件被 hook 改动、主线移动或本轮采用的来源 SHA 改变，不进入
   `source_merge_accepted`；修复后重做受影响的审查、测试和同源检查。若修复另成提交，分别登记
   原合并 SHA 与最终验收 SHA，不能继续把原合并写成最终源码。报告中的 tree、SHA、指纹、日志对齐。

## 8. M5：归档、推送与回退

- 填写下面模板，保留双方 SHA、最终 SHA、原始日志、指纹、冲突决策、未验证项和用户同意出处。
  仓库内只提交适合版本管理的规则/测试/摘要；原始本机证据存仓库外，以路径和摘要定位。
- 只有另获推送授权后，核对 `git remote get-url --push --all origin`、所有 push URL、
  `remote.origin.mirror`、`push.followTags`、`url.*.pushInsteadOf` 及 pre-push hook 副作用。
  多 push URL 意味着可能写多个目标；无法解释的额外目标/配置停止，不以 fetch URL 代替 push URL。
- 使用批准且验收过的固定 `ACCEPTED_SHA`，不使用可能已移动的 HEAD；正常非强制、非镜像推送，例如
  `git -c push.followTags=false -c remote.origin.mirror=false push --no-follow-tags --recurse-submodules=no origin "${ACCEPTED_SHA}:refs/heads/feat/target-price"`。
  核对实际写入范围，禁止 force push、自动推标签和递归推送子模块。不要用跳过 pre-push hook 规避问题。
- 推送前读回实际 push URL 上的目标分支，确认远端提交为本次候选的祖先；非快进或分叉停止，
  不自动 pull/rebase。可做 dry-run，但 dry-run 不等于真正推送，也不能消除并行更新竞态。
- 推送后针对实际 push URL 执行 `git ls-remote --exit-code "$PUSH_URL" refs/heads/feat/target-price`，
  独立核对返回完整 SHA 为 `ACCEPTED_SHA`。若远端并行前进，分别记录“本次提交已包含”和
  “远端不再精确等于本次提交”，不自动覆盖，也不笼统写“完全同步”。
  私有仓无 CI 运行不等于失败或通过，公开 CI 不能代替本地合并关卡。
- 合并完成不自动删分支、移除工作树或清扫 prunable 项。确认无未提交/未跟踪成果、无其他会话占用、
  证据已保留并获清理同意后，才使用对应工作树管理方式归档或删除；不得顺手清理其他工作树。
- 已提交合并需要撤销时，先确认是否共享及是否有后续提交，经授权优先使用新 revert 提交；普通
  双亲合并可按核实的第一父方向 `git revert -m 1 "$MERGE_SHA"`。必须重新验收并记录后续重合入影响，
  不直接改写主线历史。源码 revert 不等于安装回退或 OTA rollback。

## 9. 每轮验收模板

```text
任务/日期：
范围与排除项：
仓库/主线工作树/来源工作树：
主线分支/合并前完整 SHA：
来源分支/批准的完整 SHA/共同祖先 SHA：
远端是否刷新/实际 push URL（私密信息脱敏）/目标分支/远端读回 SHA：
现场脏文件与保护方式：
独占写入约定/已有 Git 操作/浅克隆与稀疏检出/ignored 路径冲突：
Git/Python/Qt 版本/实际 hook 路径及内容摘要/质量环境：
用户同意出处/同意的范围（合并、推送、发布分别列）：
审查发现/修复/负向对照/未闭环风险：
分支测试命令/退出码/日志：
预演结果（仅预演，不是实际验收）：
冲突文件/决策/复核：
实际合并 SHA/父提交/REVIEWED_TREE/最终验收 SHA 与 tree/工作树状态：
实际合并完整关卡命令/退出码/日志/指纹：
测试数/失败/错误/flaky/条件跳过理由/覆盖率门禁：
专项验收/用户数据保护证据：
遗留未验证项/阻断项/下一步：
推送/打包/安装/OTA/发布：未授权未执行，或逐项填写实际证据；区分推送与读回
恢复参考 SHA/实际恢复动作（未执行就写未执行）：
最终状态：
```

## 10. 过去的坑与固定防线

| 已有经历 | 本流程的固定防线 |
| --- | --- |
| v1.4.59 `.venv` 链接入库，合并后覆盖主树真实环境并形成自指 | M0 双方检查跟踪项，M1/M3 检查链接与权限，不提交环境 |
| hook 内临时 Git 操作继承 `GIT_*`，误操作真实仓库 | M3 临时仓库子进程统一清洗环境；正常 hook 不绕过 |
| 诊断分析器把未调用定义/遮蔽名称算成可达，漏回调引用 | M1 行为审查、负向对照；M4 真实合并版诊断验收 |
| 分支或 merge-tree 通过，被当成主线已通过 | M4 要求实际合并内容完整关卡、父提交及当前指纹 |
| 旧版本范围误用于新合并结果 | M4 明确版本和有效基线，不复用过期范围 |
| v1.4.69 发布树按当前分支推送，推到错误远端分支 | M5 单独授权、显式目标 refspec、远端回读 |
| 其他工作树的关卡戳或跳过覆盖率的测试被复用 | M1/M4 本树完整关卡、禁止复制戳和手工补戳 |
| 源码通过被写成装机/发布/真实场景通过 | 状态止于 `source_merge_accepted`，发布与真实验收单独进行 |
| 分支已是主线祖先，却漏看工作树未提交增量或同名测试中的独立用例 | M0 逐工作树逐文件分层；已包含、已替代、新增和语义冲突分别留下证据 |
| 新增源码/脚本/测试后继续使用旧候选、圆角截图或覆盖率凭据 | 旧候选标退役；从受影响阶段重做，不把相同版本号当同源证明 |
| 仅验证当前公开快照，忽略已公开历史中的身份元数据 | 发布前只读拉取并审计全部可达对象；精确解析允许公开的 Git 身份字段，正文及其他隐私检查不放宽 |
| 事件队列尚未绘制就断言文本，重跑绿后抹掉首败 | 保存空观测与首次日志；按测试目标获取真实帧/明确事件，复测结果和剩余风险分别记录 |
| 发行后维护流程/工具，将新主线误当已发源码 | 发行 SHA、manifest 与资产先冻结；维护另成提交，不改写发行资产或历史验收 |

可追溯实例：2026-10-15 主窗职责拆分在用户同意后，以普通双亲合并形成
`b7feb4b4efc8aaa0084e40d343bbf29ba3c14ce3`，父提交为 `0c213ff` 与 `f86e2db`。
历史验收记录见 `build/mainline-merge-20261015/ACCEPTANCE.md`（本机证据路径，可能随清理失效）；
历史用例数和覆盖率只属于当时源码，不作为未来合并的通过标准或当前证明。

## 11. 证据失效和失败恢复速查

| 发现的变化/失败 | 不能复用的结论 | 应回到哪里 |
| --- | --- | --- |
| 主线 HEAD 或本轮来源 SHA 改变 | 原合并预演与集成结果；范围改变还影响授权 | M1/M2 |
| 审查后索引树或跟踪工作文件变化 | 原树审查与测试到提交的绑定 | M3，重审并重跑受影响关卡 |
| 只有批准来源分支名称继续前进，本轮 SHA 没变 | 不自动失效，但不得带入新提交 | 保持固定 SHA，继续核对 |
| hook 路径、内容或质量环境变化 | 原执行入口和完整性判断 | M0/M1，重新核验实际执行链 |
| 关卡非零、日志丢失、仅 probe 成功或旧戳仍可用 | 本轮完整验收结论 | M1 或 M4；旧戳不是本轮日志 |
| 冲突或提交 hook 失败 | 合并完成/已验收 | 保留 MERGE_HEAD 和现场，M3 修复；经确认才 abort |
| 已提交但后续专项失败 | `source_merge_accepted` | 停止推送发布，修复提交或经授权 revert，再验收 |
| 推送失败或实际 push URL 读回失败 | 已同步；不影响已有本地源码验收事实 | M5 单独处理，不重做无关源码测试 |
| 远端、安装或发布未执行 | 远端同步、装机或发布完成 | 保留未执行，不扩大源码验收范围 |

## 12. 流程机制的可重复检查

```bash
QT_QPA_PLATFORM=offscreen ./.venv/bin/python -m unittest -v tests.test_branch_merge_workflow
```

测试使用临时 Git 仓库和无外网的本地 bare 远端，清洗继承的 `GIT_*`，不合并真实主线。
覆盖预演不移动主线、冲突/abort、合并暂停与双亲提交、正常 hook 执行及失败现场、索引与工作文件
差异、hook 改索引造成 tree 失配、ignored 环境与来源链接、linked worktree/custom hooks 安装、
未知参数/外仓环境/覆盖拒绝，以及 fetch/push URL 不同的真实推送读回。
这些测试证明所用机制与防线的边界，不替代每次真实分支的业务审查、完整回归和主线验收。
