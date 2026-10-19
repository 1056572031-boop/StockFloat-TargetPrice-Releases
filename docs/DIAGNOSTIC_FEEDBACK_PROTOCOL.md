# 诊断反馈本地协议（阶段 1）

历史阶段 1/2 记录；2026-10-19 用户已批准改为 GitHub 私有附件存储。当前客户端、收件端和管理代理实现与证据边界见 [GitHub 私有诊断反馈](GITHUB_FEEDBACK.md)。公网部署、真实用户上传及冻结包仍须独立验收。

本文件对应 `Skyrocket-诊断反馈自动化实施计划-最终版-20261017.md` 的阶段 0/1。它不是云端部署说明，也不构成原始包离开用户电脑的批准记录。

## 已实现边界

- `sf_feedback.py` 生成独立的上传专用 ZIP：数据成员先固定，`manifest.json` 最后生成，因此 manifest 不自引用 ZIP 整体哈希。
- 每个数据成员写入大小和 SHA-256；写完后重新打开 ZIP 逐成员核对。
- 拒绝路径穿越、绝对路径、反斜杠、重复或大小写碰撞、Unicode 非 NFC 名称、符号链接、加密成员、未知成员和超限成员。
- `project_snapshot()` 使用字段白名单；未知字段直接 `privacy_rejected`，不静默删除后继续。
- 包外 envelope 绑定 `idempotency_key`、包外 SHA-256、大小、同意策略版本、应用版本和本地 outbox id。
- `sf_feedback_outbox.py` 使用 SQLite WAL + `synchronous=FULL`，对象文件用同目录临时文件和 `os.replace` 写入。
- 相同幂等键和相同包恢复原记录；相同幂等键和不同包哈希拒绝；状态不可倒退；回执必须与 envelope 的哈希和大小一致。
- `reconcile_orphans()` 只报告未登记对象，不自动删除，避免把故障伪装成清理成功。
- `sf_feedback_receiver.py` 提供仅 Unix socket 的本地跨进程收件闭环：流式收件、哈希/大小重算、结构校验、SQLite 案件事务、不可覆盖回执和对象对账。
- 收件器验证同幂等键同哈希只返回原案件；同幂等键异哈希拒绝；响应丢失后重试返回原案件；对象已写案件未写报告孤儿；案件记录对应对象缺失则改成 `storage_missing`/`storage_fault`，不能继续冒充 `received`。

## 2026-10-17 的未实现边界（历史）

- 没有 HTTPS 客户端、收件服务、对象存储、案件数据库、管理代理或 LaunchAgent。
- 没有自动上传、后台上传、崩溃后上传、真实用户上传或云端删除/备份删除流程。
- 现有手工“导出诊断包”路径没有改为自动反馈路径；本模块当前不读取 QWidget、不访问网络、不请求授权。
- 阶段 0 所需的对象存储地区、保留/备份删除期、费用上限、停收责任人和首次确认文案仍待书面批准。
- 本地收件器只用于阶段 2 合成包和故障注入，未开放 TCP/HTTPS，未连接任何云账户。
- `sf_feedback_analyzer.py` 已实现离线确定性分析：不解压到工作目录、不执行成员、不联网，报告绑定 archive hash、payload/schema 版本、规则集 hash、配置 hash 和 analyzer build id；重复分析只忽略 run id、时间和机器字段后比较结论。

## 可复现验证

```bash
./.venv/bin/python -m unittest -v tests.test_feedback_protocol
./.venv/bin/python -m mypy --show-error-codes sf_feedback.py sf_feedback_outbox.py
./.venv/bin/ruff check sf_feedback.py sf_feedback_outbox.py tests/test_feedback_protocol.py
```

上述验证只证明本地协议模型，不证明真实 HTTPS、冻结包、安装、OTA、生产容量或真实用户场景。
