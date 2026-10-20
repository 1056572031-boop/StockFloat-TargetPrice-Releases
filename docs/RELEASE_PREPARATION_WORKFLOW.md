# Skyrocket 发布前准备标准流程

修订：2026-10-20。v1.4.96/v1.4.97 的实际失败、恢复和验收经验归入本流程，不新增另一套发布入口或缺陷台账。

状态：**发布前准备流程**。本文件用于把源码收口、候选构建和正式发布分开管理；它不替代
`build_release.sh`、`DELIVERY_CHECKLIST.md`、`QC_CHECKLIST.md` 或发布后真实场景验收。

适用范围：Apple Silicon、工程内 `.venv`、当前支持的 macOS 版本，以及 Beta/稳定版的每一次
新版本发布。任何一步失败、证据缺失或源码再次变化，都不能沿用后续阶段的结论。

## 一、先固定五种状态

每一轮只允许使用下面的状态，不用“差不多完成”“基本通过”代替：

| 状态 | 含义 | 可以做什么 | 不能声称什么 |
| --- | --- | --- | --- |
| `source_open` | 源码仍在修改或待复核 | 修复、补测试、整理说明 | 候选包同源、可发布 |
| `source_qualified` | 当前源码完成回归和发布前质量核验 | 生成候选包 | 安装、OTA、跨机或远端已通过 |
| `candidate_ready` | 候选 APP/DMG/ZIP/清单已生成，并有同源指纹与证据 | 复核候选，准备不可逆操作 | 已公开发布、用户已升级 |
| `released_pending_acceptance` | 已进入安装/公开发布阶段，可能只完成部分动作 | 先读回实际停点，再做安装启动、升级和回滚验收 | 已全部发布或所有真实场景已通过 |
| `complete` | 规定范围内的发布后验收全部有证据 | 归档并进入下一轮 | 超出证据范围的能力或设备已通过 |

真实场景未完成时必须保留为“未验证”；不使用 `required=false` 绕过常规安装、签名、升级、
OTA rollback、最终启动或其他正式门禁。只有用户永久政策明确列出的四个第三方浏览器专项才可
记录为 `not_applicable` 且 `required=false`，并注明“不等于通过”。

## 二、标准工作流

### P0. 建立发布边界

目标是回答“这次到底发布什么”，而不是先改版本号。

1. 确认工作树、分支、版本类型（Beta/稳定）、目标版本和本轮变更范围。
2. 阅读最近一次已发布记录和最近一次未发布/阻断记录，提取遗留的未验证项、回滚项和真实设备项。
3. 确认本轮不带入并行修改、临时输出、候选包和个人数据；`outputs/` 证据只在本地保存，除非明确属于公开交付物。
4. 为每个用户可见改动指定至少一个行为判据；无法形成包内特征时，指定源码/行为/像素判据，并写入 `release_checks.txt` 或相应测试。
5. 逐一检查相关工作树的未提交增量。来源提交已是主线祖先，不代表工作树内容都已合入；
   将增量分为相同、被主线更新实现替代、确有新增、需语义复核。同名测试文件中的不同判据也不能丢失。
6. 冻结前做既有公开历史只读审计、依赖实时检查和发布协议定向检查。`build_release.sh` 已把历史审计接在
   移动旧产物之前，`--prepare-only` 同样执行；`--local-only` 不因此新增公网发布动作。当前导出树干净不证明旧公开历史干净。

```bash
./.venv/bin/python3 tools/public_release.py --audit-public-history
./.venv/bin/python3 tools/dependency_audit.py
QT_QPA_PLATFORM=offscreen SF_RERUN_FAILED=0 ./.venv/bin/python3 tools/test_runner.py \
  --module test_public_release --module test_publish_end_to_end \
  --module test_install_guide_placeholders --module test_release_lock_cleanup \
  --module test_release_desktop_staging
```

定向检查用于尽早暴露协议、锁和阶段边界问题，不代替实际合并后的完整关卡。
历史分支不存在只是没有旧历史可审计，不等于未来导出通过；网络失败或漏洞查询不确定不能记为通过。
已占用的同名发行资产不得覆盖，改用新版本。

建议记录：

```text
版本：vX.Y.Z
阶段：Beta / stable
基线：<上一版提交或已发布候选>
本轮范围：<用户行为、修复、工具或文档>
保留未验证：<设备、系统、OTA、视觉或长期场景>
禁止混入：<并行分支、临时文件、未授权发布动作>
```

**停止条件**：版本范围不清、遗留项未分类、发现用户数据或并行改动无法隔离时，停在
`source_open`。

### P1. 源码和公开资料收口

按同一版本一次性同步，不要先打包后补文档：

1. `sf_constants.py` 的版本号是唯一版本来源。
2. `release_notes.txt` 新增本版段落；按 `1. 内容`、`2. 内容`、`3. 内容` 连续编号，有几项写几项，每项独占一行。
3. `README.md` 的版本、日期、变更正文、下载文件名和限制说明与发布说明一致。
4. 使用 `tools/render_install_guide.py` 更新 `docs/INSTALL.md`，并保留真实的签名、公证、系统和设备边界。
   包内说明稳定引用本版独立 `SHA256SUMS`，不得把包含这份说明的 ZIP 摘要写回说明，形成自引用和打包后输入漂移。
   资产生成前、生成后及摘要改变时，说明正文应相同；实际摘要在独立清单和远端下载中逐项核验。
5. 更新 `release_checks.txt`、`diag_coverage.txt` 及本轮需要的质量/逃逸记录。
6. 运行公开资料检查，不能用手工浏览代替：

```bash
./.venv/bin/python tools/public_docs_check.py
./.venv/bin/python tools/make_update_assets.py --help
```

`make_update_assets.py --help` 仅用于确认工具可用；正式资产由发版链按本版说明生成。

**停止条件**：版本、说明、安装指南或下载文件名任一不一致；说明混入未编号正文；出现
占位符、旧版本或未声明的支持范围。

### P2. 源码质量和变更覆盖

先在工程环境中确认运行时，再做完整回归：

```bash
./.venv/bin/python3 --version     # 必须是 3.14.7
./.venv/bin/python3 -c "import PyQt6"
QT_QPA_PLATFORM=offscreen SF_RERUN_FAILED=0 ./run_tests.sh
./.venv/bin/python3 tools/gate_stamp.py check
./.venv/bin/python3 tools/coverage_provenance.py check
```

`run_tests.sh` 全绿后才会写入与构建输入绑定的 `build/.gate_stamp`。如果源码、脚本、spec、
字体、构建基线或文件权限发生变化，必须重新跑完整回归。不得笼统认为文档不影响指纹：README、INSTALL 等也在当前输入名单内，
应按实际 `gate_stamp` 检查；公开资料门禁另外执行。覆盖率凭据还绑定原始解释器入口、配置和数据身份，
即使 `.venv/bin/python` 与 `.venv/bin/python3` 指向相同解释器，也不替换原路径复用凭据，更不能改写凭据消除拒绝。

先登记并清理无解释的 probe/skip/public 模式环境。非空 `SF_GUARD_PROBE`（包括字符串 `0`）不是完整回归。
保存原始退出码和整轮日志，区分故意种错的自检与最终失败；重跑通过不能抹去首败或不稳定记录。

本阶段必须同时确认：

- 静态闸门、PyObjC 桥接形状、底部间距和覆盖率棘轮通过；
- 本轮每个行为改动有对应测试或明确的专项证据；
- 逃逸复盘有真实时间边界，未发现的时刻保持 `null`，不按提交时间估算；
- 本轮真实场景缺口仍单独列为未验证，不被测试数量或覆盖率替代。

**出口**：回归绿、关卡时间戳与当前构建输入同源，状态改为 `source_qualified`。任何红灯或
同源性不明都回到 `source_open`。

### P3. 生成并固定候选

候选生成是可逆的准备动作，使用：

```bash
./build_release.sh --prepare-only
```

该模式应完成当前源码的 APP、应用 ZIP、DMG、OTA 相关候选和本地证据，但不安装、不上传、不
切换线上清单。候选完成后立即记录：

- 版本三方一致：命令行、`Info.plist`、包内 `APP_VERSION`；
- APP/ZIP/DMG 的字节数和 SHA-256；
- 构建输入指纹、候选 manifest 和质量记录；
- 包内本轮特征、import 闭包、离屏/冻结冒烟、Mach-O 和安装路径核验；
- 两代原生圆角、DMG 真挂载、卷内安装脚本元数据和其他本轮专项结果。
- 候选阶段桌面分发目录前后快照一致；桌面创建、复制新 DMG、回收旧包须晚于正式资格通过，prepared 与普通发布共用该动作。
- 在构建清理前保留版本不同、签名有效的真实旧包，供升级/恢复测试使用，不能拿同版候选冒充旧包。
- 各阶段原生采集使用独立目录；现有同名输出先按原字节、摘要和来源归档，再采集，不覆盖旧截图/result。

候选文件和证据必须放入本轮独立目录，不覆盖上一版本候选。候选生成后如修改任何构建输入，
不得继续使用旧候选，必须回到 P2 重新回归并回到 P3 重建。

**出口**：所有候选资产固定、同源、可回读，状态改为 `candidate_ready`。不能证明本轮修复
进入包内时，必须补充真实包内特征或明确的源码/行为判据，不能用上一轮特征代替。

候选后若修源码、脚本、测试或构建输入，回到 `source_open`，将旧候选明确标为退役并保留索引，重新关卡、构建和取证。
不得把旧圆角/首装/升级凭据贴到新指纹。真实 helper 回滚保留失败注入、真实旧包恢复、旧包首帧和数据哨兵；
短命签名故障载荷是受控注入，不冒充发布 ZIP 自然故障或真实用户线上现场。

### P4. 候选人工复核与发布决策

这一阶段只复核，不重新发明范围：

1. 对照 `DELIVERY_CHECKLIST.md` 和 `QC_CHECKLIST.md`，逐项检查本轮必需项、永久门禁和未验证项。
2. 确认四个发布面相互绑定：源码提交、应用包、DMG、OTA 清单/应用 ZIP。
3. 确认发布凭据、固定签名材料和远端目标可用；缺失时停止，不退回临时签名或假凭据。
   Contents 清单读写都显式绑定 `main`，不以发行仓默认 `public-source` 代替清单目标；
   本地假 HTTP 服务也须正确解析路径与查询参数，不把 `?ref=main` 当文件名。
4. 明确是否有权执行安装、上传、源码公开同步和 OTA 指针切换。没有授权时只停在 `candidate_ready`。
5. 明确绑定本轮质量记录。正式发布命令必须显式传入绝对路径，不依赖终端里残留的环境变量：

```bash
SF_QUALITY_RECORD="$PWD/build/quality-vX.Y.Z.json" ./build_release.sh --publish-prepared
```

   `build_release.sh` 会在签名、公开历史和产物核验前先检查记录存在且结构有效；这一步失败不应再进入昂贵核验。
   `qualify` 仍会在 APP、DMG、OTA ZIP/清单全部固定后执行，不能用早期结构校验代替正式资格。
6. 对候选目录、manifest、质量记录和命令日志做一次只读归档，记录当前提交和 SHA-256。

此处是发布前最后一个可安全停下的点。候选通过不等于正式发布；本机源码回归不等于冻结包、
安装、OTA rollback、跨机或 macOS 26 实机通过。

### P5. 执行正式发布

仅在 P4 明确授权后执行：

```bash
SF_QUALITY_RECORD="$PWD/build/quality-vX.Y.Z.json" ./build_release.sh --publish-prepared
```

该模式使用已验候选，不重新打包同一份源码，并执行安装、OTA/公开源码等不可逆动作。任何中途
失败都按实际停点记录，不把部分完成写成“已发布”：

- DMG/应用已生成但未上传：候选仍是本地状态；
- OTA 上传成功但清单未切换：线上仍是旧版本；
- 清单切换成功但源码同步失败：发布面不完整；
- 安装替换成功但最终启动失败：发布被阻断，不能宣称完成。

发布锁同时保护原子占位和正常/错误退出的 PID 清理，只删除本进程拥有的 PID，不杀其他任务或删除未知锁内容。
中断后先读回已发生的安装、资产和指针，再按固定 SHA/指纹/路径/字节恢复剩余阶段；不盲重跑整条链或覆盖已发资产。
发布重试时，若源码、构建输入、候选 APP、DMG、OTA ZIP/清单或质量记录发生变化，旧候选和旧收据全部退役，必须回到 P2/P3 重新回归、构建和取证；只有同源且字节未变的候选才允许使用 `--publish-prepared` 复用。
工具误判先用字段含义和正负对照独立核实，不直接去掉检查。历史审计检查全部可达对象，只有真正的
commit author/committer 或 tag tagger 字段中，精确匹配既有 GitHub 账号与获准公开支持邮箱的身份才按公开命名空间识别；
相同字节在 blob、正文、相似账号、其他联系方式或凭据命中时仍拒绝，不笼统豁免整个提交，不因误判强推改写历史。

### P6. 发布后独立验收和收口

发布动作完成后，使用独立读回而不是构建时内存中的结果：

1. 读回权威 OTA 清单、应用 ZIP、DMG、源码 ZIP 和校验清单，核对版本、签名、字节数和摘要。
2. 实际下载并校验应用 ZIP/DMG；挂载 DMG，完成安装路线和最终 `/Applications/Skyrocket.app` Cocoa 启动。
3. 复核首帧、持续存活、异常/退出码、更新状态和用户数据未被误改。
4. 按场景矩阵逐项记录通过、未验证、`not_applicable` 和阻断原因；不能用旧版证据或候选证据替代。
5. 生成发布后记录和 manifest 绑定的质量证据；只有规定范围内的真实验收全部闭环，才将状态改为 `complete`。

源码 ZIP 除版本号和压缩包摘要外，逐文件对照实际公开 SHA 的 Git blob、可执行属性和软链接。
Git archive 的 ZIP 普通文件可能没有完整 Unix 权限字段，不把格式未承载的位推测成 0644 或错误结果。
公开 CI 两项任务绑定同一公开 SHA，保留首次失败、job ID、原始日志和复测；同源复测不是首次全绿，也不证明不再复发。
绘制文案/像素检查主动取得实际帧或有界等待明确事件，不把一次 `update()+processEvents()` 当同步绘制保证，
不放宽断言让空观测通过。收口后再优化流程/工具，另立提交；不改写已发资产、刷新历史指纹或把后续主线当发行源码。

## 三、每轮最小证据包

### 自动状态、固定矩阵与发布总报告（2026-10-20）

沿用本流程五种状态与 `quality_evidence.py` 的正式政策，不建立第二套质量账本。
`build_release.sh` 在完整关卡之后创建 `build/release-state-vX.Y.Z.json`：
生成候选之前核验回归戳、覆盖率来源和公开资料，生成候选后固定 APP 目录、DMG、ZIP 和
OTA 清单的内容、权限及软链接指纹。发布既有候选必须先核验 `candidate_ready`；首次桌面覆盖、
安装或远端写入之前记为 `released_pending_acceptance`，中断不会被记录成完成。
`--skip-tests` 不能跳过新状态工具对完整回归的要求，不能与 `--publish-prepared` 混用。

源码、HEAD、已跟踪文档、构建输入或已绑定资产变化后，`validate` 拒绝旧状态。
旧状态不会静默覆盖；保留原始记录后，用 `SF_RELEASE_STATE` 指定本轮新路径，从回归和候选阶段重来。
这也是失败恢复的边界：状态为 pending 时先读回已发生动作，不能重跑发布命令覆盖同名资产。
不要把后续验收写回已固定的发布前质量记录；用另一份同源质量记录保存发布后验收。

脚本退出时自动生成 `<状态路径>.report.json` 和同名 `.md`，保留原退出码；报告失败也不冒充通过。
`report` 退出 0 只表示报告生成成功，阻断项仍看报告内容；放行必须使用 `validate` 和各阶段专用命令。
报告统一列出源码、产物、质量记录及正式场景矩阵。四个永久排除专项与 Intel 能力边界必须保留，
不适用或不支持均不等于通过；`ota_rollback` 仍是必需项。也可随时手动生成：

```bash
./.venv/bin/python3 tools/release_state.py report --state build/release-state-vX.Y.Z.json \
  --quality build/quality-vX.Y.Z.json --out build/release-summary-vX.Y.Z.json
```

有候选交付报告或独立读回报告时，在同一命令追加 `--delivery <候选交付报告>`、`--readback <独立读回报告>`；
工具核对同源后汇总，不把其他候选的通过项搬过来。缺少报告的子项保持未验证。

候选交付工具的每次运行固定记录：候选启动、首装、升级、OTA 正向、失败回滚、关键数据读回和旧版再启动。
未执行项保持 `unverified`。升级和回滚都检查带有目标价、窗口位置、高度和背景模式的隔离配置，
并在新进程使用实际旧包、升级包及回滚旧包内的解析代码核对关键字段和包内版本，缺模块时禁止回退到当前源码；
这层检查不等于冻结新旧版界面已经显示正确，后者仍须真实场景证据。
临时首装也不等于 `/Applications` 已安装或 Gatekeeper 已通过。

`public_docs_check.py` 在打包之前同时检查本版连续编号、150 字上限、README 本版正文和安装指南文件名。
安装指南继续由现有渲染器生成，不维护第二份模板；用语是否面向普通用户仍需人工审阅，不宣称机器能自动判断文案质量。

公开源码发布成功后保存 `build/publication-vX.Y.Z.json`，绑定实际公开提交、源码 ZIP 和校验文件。
发布后运行下面的独立读回：它会实际下载并核对 OTA 签名清单、应用 ZIP、DMG、源码 ZIP、校验文件，
逐文件核对源码 ZIP 与公开 Git blob/权限/软链接，核对主分支公开资料与源码包，确认同一公开 SHA 的两项 CI，
挂载下载的 DMG 并比对卷内应用，最后对同源装机应用验证签名并做 Cocoa 启动。
联网失败、CI 尚未结束、下载不一致、挂卷或最终启动失败均保留失败结果，不记为通过。

```bash
./.venv/bin/python3 tools/release_readback.py --state build/release-state-vX.Y.Z.json \
  --publication build/publication-vX.Y.Z.json --out build/readback-vX.Y.Z-attempt1.json
./.venv/bin/python3 tools/release_state.py complete --state build/release-state-vX.Y.Z.json \
  --quality build/quality-vX.Y.Z-complete.json --readback build/readback-vX.Y.Z-attempt1.json
```

读回不覆写旧报告，重试使用新输出路径。`complete` 仍要求既有质量工具验证全部有效必需真实场景，
并要求同源的七项渠道/最终启动读回全部通过；不能只修改状态文字完成发布。
读回必须发生在本轮进入发布阶段之后，时间需带时区且不能在未来；五份实际下载文件缺一份也不能收口。
公开回执、公开源码和 CI 必须指向同一提交，下载文件须逐一与候选/公开回执的原始字节摘要一致。
三种运行模式互斥，不论参数顺序如何，混用 `--local-only`、`--prepare-only`、`--publish-prepared` 都拒绝执行。
这些工具不自动推送、不自动暂停 OTA，也不包含 P3 发布后健康监控。

每个版本至少保留以下索引，文件可以分散在 `build/`、`outputs/release-candidates/` 和发布记录中，
但必须能由索引定位：

```text
release-preparation-vX.Y.Z/
  source-snapshot.json       # 分支、提交、工作树、构建输入指纹
  quality-record.json        # 回归、覆盖率、静态和逃逸复盘
  candidate-manifest.json    # APP/ZIP/DMG/OTA 资产摘要
  candidate-checks.json      # 包内、签名、圆角、DMG、安装路径等结果
  command.log                # 关键命令和退出码
  acceptance-status.md       # 发布后实际验收、未验证项和阻断项
```

证据必须绑定同一份源码和候选字节。源码变更后重新生成，不覆盖旧证据；原始失败日志保留，
修复后的通过记录另建，不把失败记录改写成通过。

## 四、快速执行表

| 阶段 | 主要命令/动作 | 成功后的状态 | 失败后的动作 |
| --- | --- | --- | --- |
| P0 边界 | 检查 git、遗留项、范围 | `source_open`（范围已锁定） | 分类缺口，不能打包 |
| P1 资料 | 更新版本、说明、安装指南，`public_docs_check` | 资料同源 | 修正文档后重查 |
| P2 质量 | `.venv` + `QT_QPA_PLATFORM=offscreen ./run_tests.sh` | `source_qualified` | 修复并重跑，旧戳不复用 |
| P3 候选 | `./build_release.sh --prepare-only` | `candidate_ready` | 保留失败证据，禁止发布 |
| P4 决策 | 只读复核、授权和证据归档 | 可进入发布 | 停在候选，不执行不可逆动作 |
| P5 发布 | `./build_release.sh --publish-prepared` | `released_pending_acceptance` | 按实际停点记录，不写 complete |
| P6 收口 | 权威读回、安装、最终启动、场景矩阵 | `complete` 或保留阻断 | 未验证项继续挂账 |

## 五、历史经验固化为规则

- v1.4.90 的磁盘空间问题说明：安装路线和 OTA 资产必须分别记账，某一路失败不能推断其他路通过。
- v1.4.91 的公开 CI/远端读回说明：CI、权威清单、CDN、客户端实际下载是不同证据源，必须分开记录。
- v1.4.92 的本机候选说明：本机安装或候选通过不等于已公开发布，也不等于跨设备验收。
- v1.4.93/v1.4.94 的质量证据说明：发布记录必须绑定源码指纹和候选 manifest，未完成真实场景继续保持未验证。
- v1.4.95 的流程改造说明：`--prepare-only` 和 `--publish-prepared` 形成明确的两段式流程；发布阶段不应为了重建同一候选而再次改变源码。
