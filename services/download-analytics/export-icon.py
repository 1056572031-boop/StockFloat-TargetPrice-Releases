"""导出下载入口用的应用图标（base64 PNG），供 build-pages.mjs 注入页面。

为什么不在网页里重画一遍标志：色块底的角是超椭圆（sf_icons.APP_ICON_PLATE_CORNER_*，
按 macOS 27 系统遮罩实测拟合），标志外框与色块的占比也只在 sf_icons 里有数。
这里直接调 render_app_icon —— 它就是包体 icon.icns 各尺寸的出图函数，
所以网页上的图标和用户在 Dock / 访达里看到的是同一份东西。

用法：QT_QPA_PLATFORM=offscreen ./.venv/bin/python export-icon.py [边长]
输出：{"light": "<base64>", "dark": "<base64>", "size": 128, "source": "sf_icons.render_app_icon"}
"""
import base64
import json
import os
import sys
from pathlib import Path

from PyQt6.QtCore import QBuffer, QIODevice
from PyQt6.QtGui import QGuiApplication

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sf_icons

_APP = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])


def png_base64(mode: str, size: int) -> str:
    # render_app_icon 的兜底是历史蓝色蜡烛，只服务于桌面程序不能无图的极端故障。
    # 网页必须与品牌同源，故先直接验证品牌 SVG 可以渲染，失败就拒绝发布页面。
    mark = sf_icons.svg_pixmap(sf_icons.brand_logo_svg(mode), size,
                               content_scale=sf_icons.APP_ICON_CONTENT_SCALE)
    if mark is None:
        raise SystemExit(f"品牌标志 {mode} 档渲染不可用，拒绝回退历史图标")
    image = sf_icons.render_app_icon(mode, size)
    if image is None or image.isNull():
        raise SystemExit(f"render_app_icon({mode}) 没有出图")
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.ReadWrite)
    if not image.save(buffer, "PNG"):
        raise SystemExit(f"render_app_icon({mode}) 存 PNG 失败")
    return base64.b64encode(bytes(buffer.data().data())).decode("ascii")


def main() -> int:
    size = int(sys.argv[1]) if len(sys.argv) > 1 else 128
    if not 32 <= size <= 512:
        raise SystemExit("边长必须在 32..512 之间")
    payload = {"light": png_base64("light", size), "dark": png_base64("dark", size),
               "size": size, "source": "sf_icons.render_app_icon"}
    print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    sys.exit(main())
