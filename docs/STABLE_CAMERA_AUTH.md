# 摄像头授权跨版本保留

## 方案与当前状态

2026-10-15 已在本机原生隔离应用上验证：同一自签名证书签署的新版本，在同一安装路径替换旧版本后，直接得到系统 `authorized` 状态，不再调用 `requestAccess`。

这不需要 Apple 开发者账号、Developer ID、公证、管理员权限或用户安装信任根。
不修改 TCC 数据库，不重置授权，不关闭 SIP 或 Gatekeeper，也不在配置里伪造“已允许”。
自签名不等于 Apple 认可开发者身份，不能解决浏览器下载的首次运行拦截问题。

源码发布链已接入固定签名；现有已安装版与线上发行资产不会被本次修改自动替换。
原生探针通过不等于正式 Skyrocket 已完成升级验收，也不等于其他 macOS 版本均已验证。

## 保持授权的条件

- 所有新版本继续使用同一签名私钥和证书。
- 应用包标识固定为 `com.skyrocket.app`，正常安装位置保持不变。
- 包内 designated requirement 同时固定包标识和证书 SHA-1 指纹，不包含易变的 cdhash；额外核验实际证书的 SHA-256。
- 安装器与 OTA 保留发行签名，不在用户机器上重新 ad-hoc 签名。
- 用户未主动撤销权限，系统未重置权限，也未受到设备管理策略限制。

临时 ad-hoc 版本迁移到固定证书版本时，身份发生了一次变化，不能保证旧授权直接迁移。
可能需要在第一个固定签名版本中再允许一次；这次迁移之后的正常升级才是保留授权的范围。

## 发布者维护

只允许一次显式初始化：

```bash
./.venv/bin/python tools/stable_signing.py init
```

初始化将私钥放入用户应用支持目录下的 `SkyrocketBuildSigning/identity.keychain-db`，
密码文件为同目录的 `keychain-password`，目录权限 0700、密码文件 0600。
私钥及密码不入仓库、不进应用、不进 DMG。对外仅发布 `tools/signing_policy.json` 中的证书指纹。
初始化不会把证书加入用户或系统信任根，并将独立钥匙串从用户搜索列表移除。

**必须离线安全备份整个私有签名目录。** 更换构建机器时恢复原材料；材料丢失时停止发布，
不要重新运行初始化来替换证书。证书轮换需要单独设计迁移，不能声称授权一定保留。

`build_release.sh` 自动执行：

1. 在搬走旧产物之前检查私有签名材料与公开证书指纹一致。
2. PyInstaller 构建后只重签应用外层，保留内嵌框架签名，不用 `--deep` 签名。
3. 在冒烟、圆角验收、DMG、安装、OTA 之前核验签名、证书和包内身份规则。
4. `--skip-build` 仅核验已有签名，不将临时签名旧包静默改签；`--skip-tests` 不跳过身份检查。

这些检查失败时拒绝发行，不回退到临时签名。

## 可复现的授权实验

此工具用独立包标识 `com.skyrocket.camera-auth-probe`，不接触 Skyrocket 配置，
不建立摄像头采集会话、不拍摄画面。只在 baseline 阶段通过按钮请求一次系统授权。
需要本机 Swift 编译工具；当前探针构建目标为 Apple Silicon，macOS 13 及以上。

```bash
./.venv/bin/python tools/verify_camera_auth_upgrade.py baseline --out build/camera-auth-acceptance
```

在测试窗口点 Authorize，并在系统框选择允许；baseline 退出后执行：

```bash
./.venv/bin/python tools/verify_camera_auth_upgrade.py upgrade --out build/camera-auth-acceptance
./.venv/bin/python tools/verify_camera_auth_upgrade.py restart --out build/camera-auth-acceptance
```

工具编译两份代码不同、版本不同的可执行文件，用同一证书签名，再在相同路径替换。
升级及重启阶段禁止调用授权请求；即使未授权也只记录失败，不再次弹授权框。
验收结论来自原生进程写回的 `authorized_without_request`，不是 `open` 命令返回 0。
每轮使用新的输出目录，保留 `result.json`、各阶段结果和旧应用，不覆盖既有证据。

正式发行仍须以真实安装包验证首次迁移、下一版本 OTA 升级、冷启动与摄像头功能。

## 依据

Apple TN2206《macOS Code Signing In Depth》的 Code Requirements 与 Self-signed Identities
章节说明指定身份规则及自签名身份；本项目不把该文档当成跨所有 macOS 版本的 TCC 保证，
摄像头授权的结论以具体系统上的原生升级实测为准。

https://developer.apple.com/library/archive/technotes/tn2206/_index.html
