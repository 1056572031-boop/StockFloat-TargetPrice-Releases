#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""管 DMG 卷内那枚「一键安装.command」**在 Finder 里长什么样**（发版链第 4 步，拷进卷根之后调用）。

两件事，一个工具：
  ① 贴图标 —— 自绘那枚"银灰驱动器 + 下箭头"，替掉系统那页写着 COMMAND 的文档缩略图；
  ② 隐藏 `.command` 后缀 —— 用户看到的是「一键安装」，不是「一键安装.command」。

为什么两件事放在同一份实现里（模块名还叫 icon，是历史）：它们写的是**同一条元数据的同一个
16 位字段**（`com.apple.FinderInfo` 的 fdFlags：自定义图标位 0x0400 与隐藏后缀位 0x0010）。
分成两个工具、各写各的，后跑的那个会把前一个的位踩掉 —— 而"图标没了"或"后缀又露出来了"
在界面上只是缩略图变回去，零报错。放一份实现里做**读-改-写**，才没有踩掉的可能。

为什么②不能靠改名实现：磁盘上的真名必须留着 `.command`，那是 LaunchServices 认它、
双击交给 Terminal 执行的依据（实测：隐藏后缀之后 `kMDItemContentType` 仍是
`com.apple.terminal.shell-script`）。隐藏的只是**显示**。

为什么这两件事都要做成链上的一步，而不是手工在 Finder 里"显示简介"勾一下：
  手工贴的东西不在版本控制里，下一轮重打包就没了；而它没了**不会有任何地方报错**，
  用户看到的还是一页写着 COMMAND 的文档缩略图（v1.4.62 就是这个样子）。

为什么图标是**画出来的**而不是一个 .icns 文件：
  ① 本仓对外是 GPL 公开快照，随包分发一枚不透明的二进制图标资源，来源没人能复核 ——
     字体那一枚已经真实踩过一次并撤过线上资产；
  ② 矢量绘制留在代码里，尺寸、颜色、构成全部可 review，改一笔就能重画；
  ③ 构图照 macOS 磁盘映像那枚（银灰立着的驱动器 + 中间下箭头 + 底部横带与指示灯），
     但**不复制 Apple 的美术资源**：形状是自己画的，取色也是自己定的。

用法
    ./.venv/bin/python tools/apply_installer_icon.py <文件路径>        # 贴图标 + 隐藏后缀
    ./.venv/bin/python tools/apply_installer_icon.py <文件> --check    # 只核验，不改
    ./.venv/bin/python tools/apply_installer_icon.py --self-test       # 变异自检
    ./.venv/bin/python tools/apply_installer_icon.py --print-sizes     # 列出各档尺寸

（没有 --dump-png：那行用法此前就写着，但 main 从来不认它 —— 未知参数一律退 2，
 想导出预览图请走 --self-test 里那套 draw(size)，或直接 import 本模块。）

判据全部落在**用户看得见的事实**上：图标是"用 NSWorkspace 把图标读回来比像素"，
后缀是"用 NSURLLocalizedNameKey 读回 Finder 会显示的名字"。
只断言"那一位写进去了"是不够的 —— 资源映射表偏移写错时图标照样登记成功而 Finder 显示旧图；
反过来元数据在、系统不认（或被用户自己的"显示所有扩展名"盖过去）时，位是"对"的，界面上什么也没变。

一处如实交代的边界：如果用户自己在「访达 → 设置 → 高级」里勾了「显示所有文件扩展名」，
他仍会看到 `.command`。那是他机器上的全局设置，优先级在包之上，本轮没有改它（也不该改）。
"""
from __future__ import annotations

import ctypes
import errno
import os
import pathlib
import struct
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 每档尺寸**各自重画**一遍（不是把 1024 缩下去）：卷里 Finder 默认图标 64~128pt，
# 缩放出来的箭头边缘会糊，小尺寸下横带与指示灯直接消失。
ICON_SIZES = (16, 32, 64, 128, 256, 512, 1024)
BODY_TOP = "#fbfbfc"
BODY_BOTTOM = "#d6d8dc"
EDGE = "#b9bcc2"
BAND = "#c7cad0"
ARROW = "#a9adb4"
LED = "#ffffff"
# 机身比例（相对画布边长）。宽度 2026-10-12 按用户看图意见从 0.52 提到 0.575
# —— 参照的那枚系统图标机身更饱满一点，0.52 在 32pt 下看起来像个窄条。
# 箭头、横带、指示灯全部相对这两个数算，改这里一处就整体跟着变。
BODY_W_RATIO = 0.575
BODY_H_RATIO = 0.76


def draw(size: int):
    """画一枚 size×size 的 QImage：银灰圆角机身 + 中间下箭头 + 底部横带 + 右下指示灯。"""
    from PyQt6.QtGui import (QBrush, QColor, QImage, QLinearGradient, QPainter,
                             QPen, QPolygonF)
    from PyQt6.QtCore import QPointF, QRectF, Qt

    img = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(QColor(0, 0, 0, 0))
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    s = float(size)
    body_w, body_h = s * BODY_W_RATIO, s * BODY_H_RATIO
    x, y = (s - body_w) / 2.0, (s - body_h) / 2.0
    grad = QLinearGradient(x, y, x, y + body_h)
    grad.setColorAt(0.0, QColor(BODY_TOP))
    grad.setColorAt(1.0, QColor(BODY_BOTTOM))
    p.setBrush(QBrush(grad))
    p.setPen(QPen(QColor(EDGE), max(1.0, s / 256.0)))
    p.drawRoundedRect(QRectF(x, y, body_w, body_h), s * 0.09, s * 0.09)
    band_h = body_h * 0.13
    p.setBrush(QBrush(QColor(BAND)))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(QRectF(x, y + body_h - band_h, body_w, band_h),
                      s * 0.05, s * 0.05)
    led_r = max(s * 0.022, 0.6)
    p.setBrush(QBrush(QColor(LED)))
    p.drawEllipse(QPointF(x + body_w * 0.82, y + body_h - band_h / 2.0), led_r, led_r)
    # 下箭头：竖杆 + 三角头，尺寸全部相对机身算，改一处不会两半脱节
    cx, cy = s / 2.0, s / 2.0 - body_h * 0.02
    aw, ah = body_w * 0.46, body_h * 0.40
    stem_w, stem_h = aw * 0.34, ah * 0.52
    p.setBrush(QBrush(QColor(ARROW)))
    p.drawRect(QRectF(cx - stem_w / 2.0, cy - ah / 2.0, stem_w, stem_h))
    p.drawConvexPolygon(QPolygonF([
        QPointF(cx - aw / 2.0, cy - ah / 2.0 + stem_h),
        QPointF(cx + aw / 2.0, cy - ah / 2.0 + stem_h),
        QPointF(cx, cy + ah / 2.0)]))
    p.end()
    return img


def _png_of(img) -> bytes:
    """QImage → PNG 字节，走 Qt 自己的编码器（不引第三方库）。

    为什么不用 constBits() 手拼：`bytes(img.constBits())` 在 PyQt6 里直接
    IndexError（voidptr 不知道自己多长），而 save(QBuffer) 这条路径不碰裸指针。
    """
    from PyQt6.QtCore import QBuffer, QIODevice
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.ReadWrite)
    if not img.save(buf, "PNG"):
        raise RuntimeError("Qt 没能把图标编码成 PNG")
    payload = bytes(buf.data().data())
    buf.close()
    if not payload:
        raise RuntimeError("PNG 字节为空")
    return payload


def _nsimage():
    """把各尺寸合成一个多档 NSImage（Finder 会按视图尺寸挑那一档）。"""
    from AppKit import NSBitmapImageRep, NSImage
    ns = NSImage.alloc().init()
    added = 0
    for size in ICON_SIZES:
        rep = NSBitmapImageRep.imageRepWithData_(_nsdata(_png_of(draw(size))))
        if rep is not None:
            ns.addRepresentation_(rep)
            added += 1
    if added == 0:
        raise RuntimeError("一枚 representation 都没配上（PNG 编码全失败）")
    return ns


def _nsimage_from(images) -> "object":
    from AppKit import NSBitmapImageRep, NSImage
    ns = NSImage.alloc().init()
    for img in images:
        rep = NSBitmapImageRep.imageRepWithData_(_nsdata(_png_of(img)))
        if rep is not None:
            ns.addRepresentation_(rep)
    return ns


def _nsdata(payload: bytes):
    from Foundation import NSData
    return NSData.dataWithBytes_length_(payload, len(payload))


# ---------------------------------------------------------------- 卷内条目的"显示名"
#
# com.apple.FinderInfo 是 32 字节，头 16 字节就是经典 FInfo：
#     fdType(4) fdCreator(4) fdFlags(2, 大端) fdLocation(4) fdFldr(2)
# 隐藏后缀那一位在 **fdFlags 的 0x0010**，自定义图标那一位是 **0x0400** —— 同一个 16 位格子。
#
# 0x0010 这个数是**量**出来的，不是抄来的：把 fdFlags 与 FXFlags 共 32 个位逐个写进一枚
# 真的「一键安装.command」，再问 `NSFileManager.displayNameAtPath:` 与
# `NSURL` 的 `NSURLLocalizedNameKey`（两个互不相干的公开读法），只有 0x0010 让显示名
# 从「一键安装.command」变成「一键安装」（探针 2026-10-12，其余 31 位两个读法都不为所动）。
# 顺带量掉两条：`/usr/bin/SetFile -a` 那一套字母在 APFS 上**根本不写 FinderInfo**（十三个
# 字母全部返回"无 FinderInfo"，别拿它当判据）；而直觉上最像的那一位（FXFlags 0x00010000）
# 是错的 —— 照着猜写下去，就是一道"位写了、界面没变"的永远看不见的门。
FINDER_INFO_XATTR = b"com.apple.FinderInfo"
FINDER_INFO_SIZE = 32
FD_FLAGS_OFFSET = 8
FLAG_HIDE_EXTENSION = 0x0010
FLAG_HAS_CUSTOM_ICON = 0x0400
LOCALIZED_NAME_KEY = "NSURLLocalizedNameKey"
# 发版链要不要隐藏后缀。唯一消费者是对外安装说明里的"双击 X"那一步（它必须按
# 用户**看得见**的名字写），所以做成一个开关而不是两处各自表述。
HIDE_EXTENSION_IN_VOLUME = True

_LIBC: "ctypes.CDLL | None" = None


def _libc() -> "ctypes.CDLL":
    """拿 libSystem 的 getxattr/setxattr。

    为什么不用 os.getxattr：本仓那个 Python 3.14 **没有**这两个函数（实测 hasattr 为假）。
    为什么不用 /usr/bin/xattr 子进程：那条路能用，但多一个外部可执行文件的依赖与一处
    编码假设；而这里要的只是"读 32 字节、改 2 字节、写回去"。
    ⚠ macOS 的 getxattr 比 Linux 多一个 `position` 参数，argtypes 写错就是"平时能用、
    换台机器偶尔崩"那一族 —— 所以签名在这里显式钉住，自检里再真跑一遍。
    """
    global _LIBC
    if _LIBC is None:
        lib = ctypes.CDLL(None, use_errno=True)
        lib.getxattr.restype = ctypes.c_ssize_t
        lib.getxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p,
                                 ctypes.c_size_t, ctypes.c_longlong, ctypes.c_uint32]
        lib.setxattr.restype = ctypes.c_int
        lib.setxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p,
                                 ctypes.c_size_t, ctypes.c_longlong, ctypes.c_int]
        _LIBC = lib
    return _LIBC


def read_finder_info(path: pathlib.Path) -> bytes:
    """读那条元数据。文件上根本没有时返回 b""（不是 0 字节的元数据，是**没有**）。"""
    lib = _libc()
    buf = ctypes.create_string_buffer(FINDER_INFO_SIZE)
    n = lib.getxattr(os.fsencode(str(path)), FINDER_INFO_XATTR, buf,
                     FINDER_INFO_SIZE, 0, 0)
    if n < 0:
        err = ctypes.get_errno()
        if err in (getattr(errno, "ENODATA", 96), getattr(errno, "ENOATTR", 96)):
            return b""
        raise RuntimeError("读 com.apple.FinderInfo 失败：%s" % os.strerror(err))
    return buf.raw[:n]


def write_finder_info(path: pathlib.Path, blob: bytes) -> None:
    lib = _libc()
    payload = bytes(blob)
    buf = ctypes.create_string_buffer(payload, len(payload))
    if lib.setxattr(os.fsencode(str(path)), FINDER_INFO_XATTR, buf,
                    len(payload), 0, 0) != 0:
        raise RuntimeError("写 com.apple.FinderInfo 失败：%s"
                           % os.strerror(ctypes.get_errno()))


def finder_flags(path: pathlib.Path) -> int:
    raw = read_finder_info(path)
    if not raw:
        return 0
    if len(raw) < FD_FLAGS_OFFSET + 2:
        raise RuntimeError("com.apple.FinderInfo 只有 %d 字节，装不下 fdFlags —— "
                           "不猜偏移，直接说" % len(raw))
    return struct.unpack_from(">H", raw, FD_FLAGS_OFFSET)[0]


def hide_extension(path: pathlib.Path) -> None:
    """把隐藏后缀那一位**并**进去，其余字节原样保留。

    为什么必须读-改-写而不是直接写一枚"只有隐藏位"的 32 字节：自定义图标位就在同一个
    16 位格子里，整块覆盖等于把刚贴上的图标踩掉（界面上退回文档缩略图，零报错）。
    自检里 S8 专门种这一处错。
    """
    raw = read_finder_info(path) or b"\x00" * FINDER_INFO_SIZE
    if len(raw) < FD_FLAGS_OFFSET + 2:
        raise RuntimeError("com.apple.FinderInfo 形状不认识（%d 字节），不改" % len(raw))
    flags = struct.unpack_from(">H", raw, FD_FLAGS_OFFSET)[0]
    if flags & FLAG_HIDE_EXTENSION:
        return                                    # 幂等：重复跑不产生第二次写
    patched = (raw[:FD_FLAGS_OFFSET]
               + struct.pack(">H", flags | FLAG_HIDE_EXTENSION)
               + raw[FD_FLAGS_OFFSET + 2:])
    write_finder_info(path, patched)


def displayed_name(path: pathlib.Path) -> str:
    """Finder 会给这个文件显示的**那个名字**。取不到就返回空串，由调用方判红。"""
    from Foundation import NSURL
    url = NSURL.fileURLWithPath_(str(path))
    got = url.resourceValuesForKeys_error_([LOCALIZED_NAME_KEY], None)
    vals = got[0] if isinstance(got, tuple) else got
    shown = (vals or {}).get(LOCALIZED_NAME_KEY)
    return "" if shown is None else str(shown)


def finder_info_extension_hidden(path: pathlib.Path) -> bool:
    """确认 FinderInfo 的后缀隐藏位已写入；它是显示名检查的底层对照。"""
    raw = read_finder_info(path)
    if not raw or len(raw) < FD_FLAGS_OFFSET + 2:
        return False
    flags = struct.unpack_from(">H", raw, FD_FLAGS_OFFSET)[0]
    return bool(flags & FLAG_HIDE_EXTENSION)


def extension_hidden(path: pathlib.Path) -> bool:
    """判据同时要求 FinderInfo 写入隐藏位，并且显示名去掉了真后缀。"""
    if not finder_info_extension_hidden(path):
        return False
    name = pathlib.Path(path).name
    suffix = pathlib.Path(name).suffix
    shown = displayed_name(path)
    if not shown or not suffix or shown == name:
        return False
    return shown == name[: len(name) - len(suffix)]


def volume_displayed_name(name: str) -> str:
    """发版链跑完第 4 步之后，用户在卷里**看到**的名字 —— 对外说明按它写。

    这里不调 `displayed_name()`：仓库里 `installer/一键安装.command` 是源文件，
    扩展名从来没被隐藏过（xattr 不进 git），隐藏只发生在 DMG 那份副本上。
    """
    if not HIDE_EXTENSION_IN_VOLUME:
        return name
    suffix = pathlib.PurePosixPath(name).suffix
    return name[: len(name) - len(suffix)] if suffix else name


def read_back_signature(path: pathlib.Path) -> "bytes | None":
    """把系统**实际解析出来**的那枚图标读回来，按同一套比例采样。

    为什么必须读回来比：setIcon_ 返回 True 只说明 API 收下了，不说明 Finder 认。
    资源叉写坏时它照样返回成功，界面上仍是旧图标 —— 那是"闸门说假话"那一族。
    采样按 rep 自身像素比例换算，不重采样成固定 128：少一步转换就少一处会错的地方。
    """
    from AppKit import NSBitmapImageRep, NSWorkspace
    img = NSWorkspace.sharedWorkspace().iconForFile_(str(path))
    if img is None:
        return None
    reps = list(img.representations())
    if not reps:
        return None
    want = min(reps, key=lambda r: abs(int(r.pixelsWide()) - 128))
    img.setSize_((int(want.pixelsWide()), int(want.pixelsHigh())))
    tiff = bytes(img.TIFFRepresentation())
    back = NSBitmapImageRep.imageRepWithData_(_nsdata(tiff))
    if back is None:
        return None
    return _sample(back)


def _sample(rep) -> bytes:
    """取一条穿过箭头的竖直采样（16 个点 × RGBA），够区分"我们的图"与"系统默认图"。"""
    from AppKit import NSColorSpace
    w, h = int(rep.pixelsWide()), int(rep.pixelsHigh())
    if not w or not h:
        return b""
    rgb_space = NSColorSpace.deviceRGBColorSpace()
    out = bytearray()
    for i in range(16):
        # 沿机身竖直中线取 16 个点：这条线一定穿过箭头的杆与头，
        # 而系统默认的文档图标在同一个位置是空白或文字，两者采不出同一条曲线
        frac = (8 + i * (112 / 15.0)) / 128.0
        x = min(w - 1, max(0, int(round(w / 2.0))))
        y = min(h - 1, max(0, int(round(frac * h))))
        c = rep.colorAtX_y_(x, y)
        if c is None:
            return b""
        # colorAtX_y_ 给的是**带标签**的 NSColor（可能是灰度/索引色空间），
        # 直接问它要 red 会 AttributeError —— 必须先转到设备 RGB 再取分量
        rgb = c.colorUsingColorSpace_(rgb_space)
        if rgb is None:
            return b""
        out += bytes((int(rgb.redComponent() * 255), int(rgb.greenComponent() * 255),
                      int(rgb.blueComponent() * 255), int(rgb.alphaComponent() * 255)))
    return bytes(out)


def expected_signature() -> bytes:
    """期望指纹：直接对**自己画出来的**那一帧做同样的采样，不另存基线文件。"""
    from AppKit import NSBitmapImageRep
    rep = NSBitmapImageRep.imageRepWithData_(_nsdata(_png_of(draw(128))))
    if rep is None:
        raise RuntimeError("期望帧读不回来（PNG 编码坏了）")
    return _sample(rep)


def apply_icon(path: pathlib.Path) -> None:
    from AppKit import NSWorkspace
    ok = NSWorkspace.sharedWorkspace().setIcon_forFile_options_(_nsimage(), str(path), 0)
    if not ok:
        raise RuntimeError("NSWorkspace.setIcon 返回 False（这台机器贴不了自定义图标？）")


def apply_look(path: pathlib.Path) -> None:
    """发版链第 4 步真正要的那一下动作：贴图标，再把 `.command` 从显示里去掉。

    顺序不能反：`setIcon_` 自己会写 fdFlags 的 0x0400，隐藏后缀那一步是读-改-写，
    放在后面才能看见它；放前面就等于让图标去决定"要不要露后缀"。
    """
    apply_icon(path)
    if HIDE_EXTENSION_IN_VOLUME:
        hide_extension(path)


def check_icon(path: pathlib.Path) -> tuple[bool, str]:
    """判据只有一条：Finder 现在给这个文件显示的图标，是不是本轮画出来的那一枚。

    为什么不去查 FinderInfo 的 kHasCustomIcon 位或资源叉长度：那些都是"登记痕迹"，
    贴错图（比如上一轮的图残留、或被人换成别的图标）时它们照样是"有"。
    本机的 Python 3.14 连 os.getxattr 都没有，读回来比像素反而是最短的路。
    """
    if not path.is_file():
        return False, "文件不存在：%s" % path
    got = read_back_signature(path)
    if not got:
        return False, "系统没能把这枚图标读回来（资源叉形状不对，Finder 会显示旧图）"
    want = expected_signature()
    if not want:
        return False, "期望帧采样失败，这条判据现在没有依据"
    # 允许小偏差：Finder 会给图标加阴影/边缘抗锯齿，逐字节相等太脆
    diff = sum(abs(a - b) for a, b in zip(got, want)) / float(len(want) or 1)
    if diff > 24.0:
        return False, ("读回的图标与本轮画出来的不像（平均色差 %.1f/通道，"
                       "多半是系统还在显示旧图标）" % diff)
    return True, "图标已贴上且能被 Finder 读回（平均色差 %.1f/通道）" % diff


def check(path: pathlib.Path) -> tuple[bool, str]:
    """卷内那条目在 Finder 里的样子：图标 + 显示名，两条都要成立。

    为什么两条合在同一个 check 里：发版链与 R2 各只调用它一次（挂载真 DMG 读回），
    分成两个入口就等于给"漏掉一条"留了位置 —— 而漏掉任何一条，界面上都只是"变回原样"，
    没有任何地方会报错。
    """
    icon_ok, icon_note = check_icon(path)
    if not icon_ok:
        return False, icon_note
    # 发版链的卷内条目始终必须隐藏 `.command`：对外文案开关不能削弱真实验收。
    if not extension_hidden(path):
        return False, ("%s；但 `.command` 还露在显示名里（卷里显示为「%s」）—— "
                       "隐藏后缀那一步没跑，或被后来的整块覆盖踩掉了"
                       % (icon_note, displayed_name(path) or path.name))
    return True, "%s；后缀已隐藏，卷里显示为「%s」" % (icon_note, displayed_name(path))


def _blank_icon(size: int):
    """种错用的"另一枚图"：实心白方块，与本轮画的驱动器图标在采样线上必然不同。"""
    from PyQt6.QtGui import QColor, QImage, QPainter
    img = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(QColor(0, 0, 0, 0))
    p = QPainter(img)
    p.fillRect(0, 0, size, size, QColor(255, 255, 255, 255))
    p.end()
    return img


def self_test() -> int:
    """正当输入判绿 + 九处种错必须判红。分两类看：

      S1/S3/S4/S5 —— 图标那一半的老判据（v1.4.63 立起来的）；
      S6~S10 —— 本轮「隐藏 `.command` 后缀」补上的，每一条都对应一种
                「元数据写了、界面上什么也没变」或「另一件事被顺手踩掉」的真错法。
    """
    import shutil
    import tempfile
    bad = []
    work = pathlib.Path(tempfile.mkdtemp(prefix="sf_icon_selftest_"))

    def fresh(tag: str) -> pathlib.Path:
        """每档一个**子目录**里的同名文件：图标按路径有缓存，换路径才测得出「位被踩掉之后
        Finder 改不改口」；名字必须保持一致，否则 S2 那条「显示名 == 一键安装」没法写。"""
        d = work / tag
        d.mkdir(parents=True, exist_ok=True)
        p = d / "一键安装.command"
        p.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
        os.chmod(p, 0o755)
        return p

    try:
        target = fresh("S1")
        # S1 什么都没做（既没图标也没隐藏后缀）：必须判红（否则这条判据看不见「外观没了」）
        ok, note = check(target)
        if ok:
            bad.append("S1 什么都没做的文件被判成通过：%s" % note)

        # S2 正当输入（贴图标 + 隐藏后缀）：必须判绿（判据不许把好包误杀）
        apply_look(target)
        ok, note = check(target)
        if not ok:
            bad.append("S2 刚做完外观的条目被判红：%s" % note)
        elif displayed_name(target) != "一键安装":
            bad.append("S2 判绿了，可显示名不是「一键安装」而是「%s」" % displayed_name(target))
        # 给"切名字"那份实现上一道真行为锁：它算出来的名字必须等于**系统实际会显示的**那个。
        # 没有这一条，把它改成原样返回，对外安装说明里那句「双击 X」就悄悄和卷里对不上了
        # （说明与元数据两处各自表述，而界面上不会有任何地方报错）。
        if volume_displayed_name(target.name) != displayed_name(target):
            bad.append("S2 volume_displayed_name(%r)=%r，可系统实际显示的是 %r"
                       % (target.name, volume_displayed_name(target.name),
                          displayed_name(target)))

        # S3 贴的是**另一枚图**（实心白方块）、后缀已隐藏：必须判红。
        #    这一条是给判据本身做分辨力测试 —— 只看「有没有自定义图标」的写法在这里
        #    会放行，而用户看到的是「后缀藏了、图还是不对」这种半吊子。
        wrong = fresh("S3")
        from AppKit import NSWorkspace
        NSWorkspace.sharedWorkspace().setIcon_forFile_options_(
            _nsimage_from([_blank_icon(s) for s in (128, 256)]), str(wrong), 0)
        hide_extension(wrong)
        ok, note = check(wrong)
        if ok:
            bad.append("S3 换成另一枚图标后仍判通过：%s" % note)

        # S4 尺寸表被清空：apply 必须失败，不能「静默贴一枚空图」
        saved = globals()["ICON_SIZES"]
        try:
            globals()["ICON_SIZES"] = ()
            try:
                apply_look(target)
                bad.append("S4 尺寸表清空后 apply_look 居然成功了（空图标会安静上线）")
            except Exception:
                pass
        finally:
            globals()["ICON_SIZES"] = saved

        # S5 路径不存在：判红而不是抛穿（发版脚本按文件名发现脚本，拼错要能看清）
        ok, note = check(work / "根本没有这个文件.command")
        if ok:
            bad.append("S5 不存在的文件被判通过")

        # S6 **只贴图标、忘了隐藏后缀**（= 用户 2026-10-12 报的那个样子）：必须判红，
        #    而且理由要点名「后缀」，否则下一轮读日志的人不知道该去补哪一步。
        only_icon = fresh("S6")
        apply_icon(only_icon)
        ok, note = check(only_icon)
        if ok:
            bad.append("S6 只贴图标、后缀仍露着却判通过：%s" % note)
        if "后缀" not in note:
            bad.append("S6 判红了，但理由没点名是后缀的问题：%s" % note)

        # S7 两位共存：隐藏后缀**不许**把自定义图标位（0x0400）一起写没
        with_icon = finder_flags(only_icon)
        hide_extension(only_icon)
        after = finder_flags(only_icon)
        if not (with_icon & FLAG_HAS_CUSTOM_ICON):
            bad.append("S7 贴完图标却没读到 0x0400：%04x" % with_icon)
        if not (after & FLAG_HIDE_EXTENSION) or not (after & FLAG_HAS_CUSTOM_ICON):
            bad.append("S7 隐藏后缀之后 fdFlags 只剩一位：%04x（应为 0x0410）" % after)

        # S8 把读-改-写换成「整块只写自己那一位」：图标判据必须当场红。
        #    这一条是给 S7 那处合并写上锁 —— 谁哪天图省事直接写一枚新 blob，
        #    界面上就是「后缀藏了、图标没了」，而它不会在任何地方报错。
        clobber = fresh("S8")
        apply_look(clobber)
        write_finder_info(clobber, struct.pack(">4s4sH4sHI12s", b"", b"",
                                              FLAG_HIDE_EXTENSION, b"\0" * 4, 0, 0,
                                              b"\0" * 12))
        ok, note = check(clobber)
        if ok:
            bad.append("S8 整块覆盖踩掉图标位之后仍判通过：%s" % note)

        # S9 幂等：重复跑不许改变元数据（发版链把同一步跑两遍是常态）
        before = read_finder_info(clobber)
        hide_extension(clobber)
        hide_extension(clobber)
        if read_finder_info(clobber) != before:
            bad.append("S9 重复跑 hide_extension 改变了元数据")

        # S10 那一位是**量**出来的：换成「直觉上最像」的 FXFlags 0x00010000 必须仍判红。
        #     没有这一条，常量被改成任何别的数字，这套自检都照样绿。
        guessed = fresh("S10")
        apply_icon(guessed)
        write_finder_info(guessed, struct.pack(">4s4sH4sHI12s", b"", b"", 0, b"\0" * 4, 0,
                                              0x00010000, b"\0" * 12))
        if extension_hidden(guessed):
            bad.append("S10 写 FXFlags 0x00010000 也被认成「隐藏了后缀」—— 那一位是猜的")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    for line in bad:
        print("!! " + line)
    if bad:
        return 1
    print("== apply_installer_icon 自检通过：正当输入判绿 + 九处种错全部判红 ==")
    return 0


def main(argv: list[str]) -> int:
    # 未知参数退 2（AGENTS §3.1 链上规矩）：默认动作是"改文件"，打错参数不许落到它上面
    args = [a for a in argv[1:] if not a.startswith("--")]
    flags = [a for a in argv[1:] if a.startswith("--")]
    known = {"--check", "--self-test", "--print-sizes"}
    unknown = [f for f in flags if f not in known]
    if unknown:
        print("!! 不认识这些参数：%s\n   用法：apply_installer_icon.py <文件> [--check]"
              " | --self-test | --print-sizes" % "、".join(unknown), file=sys.stderr)
        return 2
    if "--self-test" in flags:
        return self_test()
    if "--print-sizes" in flags:
        for size in ICON_SIZES:
            print("   %d×%d" % (size, size))
        return 0
    if len(args) != 1:
        print("!! 需要且只需要一个文件路径，收到 %d 个：%s" % (len(args), args),
              file=sys.stderr)
        return 2
    target = pathlib.Path(args[0])
    if "--check" in flags:
        ok, note = check(target)
        print("%s %s" % ("✓" if ok else "✗", note))
        return 0 if ok else 1
    apply_look(target)
    ok, note = check(target)
    print("%s %s" % ("✓" if ok else "✗", note))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
