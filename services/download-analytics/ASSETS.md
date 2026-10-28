# 下载页素材来源登记

`public/assets/` 里的每个文件都必须在这里有一行：来源、许可或授权状态、怎么生成。
`tests/worker.test.mjs` 会核对两边一致，新增素材不登记，测试会红。

## 未取得授权的素材（2026-10-28 按用户决定从 B25 效果图原样换回）

2026-10-27 的法律风险走查曾把下面三类素材撤掉。2026-10-28 用户明确要求素材也原样换回 B25，并已被告知：
`services/` 会进入公开的 GPL 源码快照（`tools/public_release.py`），把它们留在这里等于公开分发。
这些素材**没有取得授权，也没有核实许可**。公开发布源码快照、或把这一版部署到生产站点之前，应先换成可授权素材。

- 真实股票行情：某只 A 股 2026-10-27 09:30 至 13:46 的 168 个分钟点（腾讯行情源，`sf_quote.fetch_minute`）。交易所和行情商对再分发有授权约束，授权未确认。画面里的名称和代码已替换为占位的“股票名称 / 038492”（2026-10-28 用户要求），但**价格走势和价位仍是真实的**，熟悉该股的人仍可能认出，所以这不等于脱敏，也不等于有了授权。
  名称和代码是烧在像素里的（视频、封面图、四张场景图），原始行情数据与渲染帧已不在，没法重渲：做法是按新名称重渲标签矩形，再逐帧覆盖；第 3、4 张用系统玻璃截屏缓存无损重做，第 1、2 张在已有成品上做差分补丁（第 3 张上验证过补丁与无损真值的差）。发布前须用系统文字识别逐张确认旧名称和旧代码读不出来。
- 苹果素材：场景图里的菜单栏、程序坞玻璃底座（程序坞里的系统应用图标已在 2026-10-28 换成淡色纯色块和 Skyrocket 自己的图标，不再含苹果图标）、SF Symbols 导出图、macOS 文件图标（PDF、DMG、ZIP）；小窗里的中文由苹方（PingFang SC）渲染，网站图片是否允许使用该字体未核实。第三张全屏图没有菜单栏和程序坞，但控制条用了 SF Symbols。

桌面壁纸不再是苹果的：2026-10-28 用户从候选里选定四张，换成下面“壁纸来源”表里的照片。第三张图原先用的影视单帧也在同一天换掉了（现在是壁纸当作全屏播放的画面），不再有需要署名的第三方影视画面。

上一版合规素材（虚构标的“示例股份 000000”首屏图、Unsplash 风光壁纸、HTML/CSS 自绘场景）在提交 `e79022a` 里，随时可以换回。`asset-sources/render-hero.py.txt` 仍是那张首屏图的渲染脚本。

| 文件 | 来源 | 许可或授权状态 | 生成方式 |
| --- | --- | --- | --- |
| `landing.css` | 本项目自写 | GPLv3，随源码 | 手写 |
| `landing.js` | 本项目自写 | GPLv3，随源码 | 手写 |
| `atmo.svg` | 本项目自绘 | GPLv3，随源码 | 固定种子 `Random(11)` 合成的上行 K 线装饰，不取自任何真实行情 |
| `installer-icon.png` | 本项目自绘 | GPLv3，随源码 | `tools/apply_installer_icon.py` 的 `draw()`，代码画出 |
| `hero-replay-60fps.webm` | 产品离屏渲染器 + 某只 A 股 2026-10-27 真实分钟线 | 未取得授权（行情再分发；苹方字体） | 13:46 前的真实点逐个回放，900 帧 60fps，VP9 透明 WebM，给 Chrome 等；标签区已覆盖成“股票名称 / 038492”后重新编码（比原版多一代有损压缩） |
| `hero-replay-60fps-hevc.mov` | 同上 | 未取得授权（同上） | 同一回放的 HEVC 透明 MOV，给 Safari；由上面覆盖后的 WebM 解码帧重新编码（HEVC 的透明层 ffmpeg 解不出来，所以不是从原 MOV 改的） |
| `hero-replay-poster.webp` | 同上 | 未取得授权（同上） | 同一回放的静止帧：视频封面、减少动态效果时的替代图、第 3 步示意图；无损，标签区已覆盖 |
| `scene-1-switch.webp` | 产品渲染的小窗 + 壁纸 12（见“壁纸来源”）+ 苹果菜单栏、程序坞玻璃底座；程序坞图标为 8 块自绘淡色纯色块（macOS 图标轮廓的圆角曲线）和 Skyrocket 的 `icon.png` | 未取得授权（苹果素材；小窗里是真实行情走势）；壁纸见下表 | 第一张场景图：小窗浮在其他窗口上，2940×1912 |
| `scene-2-alert.webp` | 同上，壁纸 08；程序坞图标同第一张（自绘淡色纯色块 + Skyrocket 的 `icon.png`）；通知横幅在系统玻璃上另垫一层 30% 不透明的黑色衬底（2026-10-28，提高文字对比度） | 未取得授权（苹果素材；真实行情走势）；壁纸见下表 | 第二张场景图：桌面右上角弹出到价提醒。横幅文字是白字，对着无文字的同一张衬底图量，标题行与副标题行最不利像素的对比度都是 7.6:1（垫衬底前 4.4:1）；横幅以外的像素没有改动，只有重新编码的压缩噪点 |
| `scene-3-fullscreen.webp` | 产品真实右键菜单渲染 + 壁纸 13 当作全屏播放的画面（2.35:1，上下黑边）+ SF Symbols 控制条 | 未取得授权（苹果 SF Symbols；真实行情走势）；壁纸见下表 | 第三张场景图：全屏播放时小窗仍在画面上 |
| `scene-4-hide.webp` | 产品渲染的小窗 + 壁纸 05 + 苹果菜单栏、程序坞玻璃底座；程序坞图标同第一张（自绘淡色纯色块 + Skyrocket 的 `icon.png`） | 未取得授权（苹果素材；真实行情走势）；壁纸见下表 | 第四张场景图：有人靠近时小窗收起 |
| `file-pdf.png` | macOS 系统文件图标（`NSWorkspace` 取 `UTType.pdf`） | 未取得授权（苹果素材） | 第 1 步示意图，192×192 |
| `file-dmg.png` | macOS 系统文件图标（`NSWorkspace` 取本机分发目录里真 DMG 的图标） | 未取得授权（苹果素材） | 第 1 步示意图，192×192 |
| `file-zip.png` | macOS 系统文件图标（`NSWorkspace` 取 `UTType.zip`） | 未取得授权（苹果素材） | 第 1 步示意图，192×192 |

## 壁纸来源（四张场景图的桌面背景，2026-10-28 用户选定）

四张都是维基共享资源（Wikimedia Commons）上标为 CC0 的照片，没有嵌进任何商标或水印。下表的许可是**读自 Commons 文件页的标注，没有回到原站（Unsplash、Pixel.la）再核对**；Unsplash 的照片只有在 2017 年改许可之前上传的才是 CC0，这一点也没有逐张核实。CC0 不要求署名，这里仍登记作者以便追溯。

| 场景图 | 壁纸 | 作者 | 许可（Commons 标注） | 文件页 |
| --- | --- | --- | --- | --- |
| `scene-1-switch.webp` | 12 日落云彩 Colorful sky during sunset (Unsplash).jpg | Rowan Heuvel | CC0 | https://commons.wikimedia.org/wiki/File:Colorful_sky_during_sunset_(Unsplash).jpg |
| `scene-2-alert.webp` | 08 天空与云 Landscape-nature-sky-clouds (23958729879).jpg | www.Pixel.la Free Stock Photos | CC0 | https://commons.wikimedia.org/wiki/File:Landscape-nature-sky-clouds_(23958729879).jpg |
| `scene-3-fullscreen.webp` | 13 红色天空 Red sky sunset (Unsplash).jpg | Quino Al | CC0 | https://commons.wikimedia.org/wiki/File:Red_sky_sunset_(Unsplash).jpg |
| `scene-4-hide.webp` | 05 沙丘暮色 Desert Skies (Unsplash).jpg | Jared Evans | CC0 | https://commons.wikimedia.org/wiki/File:Desert_Skies_(Unsplash).jpg |

没有文件的素材（由 `src/landing.mjs` 与 `landing.css` 直接画出）：

- 页面里的箭头、对勾、下载图标、第 2 步的“应用程序”文件夹是内联 SVG 路径，本项目自绘。

构建时生成、不放进 `public/` 的：

| 文件 | 来源 | 生成方式 |
| --- | --- | --- |
| `app-icon.png` | 本项目自绘 | `build-pages.mjs` 调 `export-icon.py`，即 `sf_icons.render_app_icon("light", 128)`，与包体 `icon.icns` 同一个出图函数 |
