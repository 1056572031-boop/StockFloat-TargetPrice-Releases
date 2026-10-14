# 第三方开源组件声明（v1.2.22 起随分发提供）

本应用（Skyrocket，v1.4.54 及更早名为「实时股价-目标价版」）基于以下开源软件构建，按各自许可条款使用：

## PyQt6 — GPLv3

- 项目：Riverbank Computing 的 PyQt6 绑定（Python 版 Qt6）
- 许可证：GNU General Public License v3（GPLv3）
- 官网：https://www.riverbankcomputing.com/software/pyqt/

### 对分发者的影响（重要）

PyQt6 采用 **GPLv3** 许可证。GPLv3 要求：将包含 GPL 组件的程序对外分发时，
必须**以 GPLv3 兼容的许可证**开源该程序对应的完整源码，并在分发物中提供本声明与
许可证文本。因此：

- **个人使用 / 内部自用**：不受分发义务约束，可自由使用。
- **对外分发 / 提供给他人使用 / 商业发行**：需按 GPLv3 提供完整对应源码
  （本工程所有 .py 源码与构建脚本均包含在源码目录中），并附带 GPLv3 许可证全文
  （https://www.gnu.org/licenses/gpl-3.0.html）。

> 本声明仅为提示，不构成法律意见。具体义务请以 GPLv3 许可证原文及专业法律咨询为准。

## 其它说明

- 应用包内 `licenses/` 同步携带本轮实际安装的 PyQt6、Qt、SIP、PyNaCl、libsodium、
  CFFI 与 PyObjC 许可文件；许可证来自构建依赖的原始发行元数据，不使用手写摘要替代原文。
- 本轮未购买或启用 Developer ID 商业签名；系统公证与许可材料是不同事项。

- 本项目自身源码：GPL-3.0-only，完整许可见随包 `licenses/LICENSE` 或源码根目录 `LICENSE`。
  每次发行均提供对应源码 ZIP，公开源码位于
  https://github.com/1056572031-boop/StockFloat-TargetPrice-Releases/tree/public-source 。
- 界面图标等资源：本应用自有资源，不涉及第三方许可。

## 内置数字字体 — D-DIN（SIL Open Font License 1.1）

- 字体：`D-DIN`（Regular `fonts/D-DIN.otf`、Bold `D-DIN-Bold.otf`），用于价格、涨跌幅、
  代码与最高/最低标注的数字部分；中文与标签仍用系统苹方（不随包分发）。
- 版权与许可：`Copyright (c) 2017, Datto Inc. (fonts@datto.com)`，以 **SIL Open Font
  License, Version 1.1** 授权，保留字体名（Reserved Font Name）为 `D-DIN`。许可原文随包
  置于 `fonts/OFL-D-DIN.txt`，并按 OFL 要求在分发物中一并携带。
- 取得来源：Datto 官网字体页（`datto.com/fonts/d-din`）现已下线，本仓库所用文件取自
  Font Squirrel 的 `d-din` 字体包；文件内嵌的版权与许可字段即权利人 Datto 本人的
  OFL 声明，逐表可核。校验值（sha256）：
  - `D-DIN.otf` 6d67834e2a76646c456c087ce42a6bd6b6b0c85c88dd9918618a8b4c563c2bdf
  - `D-DIN-Bold.otf` b0f96a3730041605b139ca2d15e29a36c55e49058ba2b72ee4d09b5e4ca210c1
  - `OFL-D-DIN.txt` 945bb366b143a8c561daf86892bb838ee456c58585afc0868e2d3f2cf4f8b4f7
- 历史说明：v1.2.24 起随包的是名为 `D-DIN-PRO` 的两个 ttf，系第三方重打包件
  （版权行被替换为个人署名并附一份无授权的 OFL 声明），权利链不成立，已自本版本起
  从仓库与分发物中移除。替换后的官方 D-DIN 与之逐像素同形（27pt 下常用串差异 0.01%~0.04%，
  仅为抗锯齿），界面观感与排版不变。
