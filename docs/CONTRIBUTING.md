# 修改与构建

源码采用 GPL-3.0-only。请先阅读 README 的隐私与数据使用边界；修改软件不意味着取得行情供应商的授权。

运行与回归均使用工程 `.venv`（Python 3.14.7），精确依赖见 `requirements.lock`。执行 `QT_QPA_PLATFORM=offscreen ./run_tests.sh`，全量回归通过后才可打包。

`StockFloat.spec` 是独立打包配置。可执行 `.venv/bin/python -m PyInstaller --noconfirm StockFloat.spec` 构建本地应用。正式发行另需维护者自己的更新签名密钥及 GitHub 权限，私钥不在公开源码中。

每个新版本都必须同步更新 README 的版本、日期与「这版改了什么」，并为 `release_notes.txt` 新增自己的版本段；两份本版更新正文须一致。更新正文必须从 `1. ` 起连续编号，每项独占一行，有几项写几项，不写大段文字、不凑项；含标题与编号去空白不超过 100 字。生成 OTA 资产前自动检查编号，不合规拒绝签发。用 `tools/render_install_guide.py` 刷新安装说明，再运行 `tools/public_docs_check.py`。版本、内容、许可或安全说明缺失会阻止打包。

正式发行脚本 `build_release.sh` 会携带文档到应用和 DMG，生成干净的公开对应源码并扫描全部可达历史。公开源码不包含内部交付报告、回退备份、运行数据、个人路径或签名私钥；发布使用独立的 `public-source` 分支，不公开私有开发历史。

提交故障修复时给出最少复现和行为测试，避免无关重构。安全问题按 SECURITY.md 私下报告，不在 PR 中加入真实凭据或个人数据。
