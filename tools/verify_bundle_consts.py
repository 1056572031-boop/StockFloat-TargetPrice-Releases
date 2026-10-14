#!/usr/bin/env python3
"""解包已打包 .app 的内嵌 PYZ，读取模块顶层常量的**实际值**。

用途：确认某次改动是否真的进了产物（改常量值、改版本号这类改动，
     只看版本字符串不够，得看到常量本身的值）。

用法:
    ./.venv/bin/python tools/verify_bundle_consts.py <app路径> <模块名> <常量名>...
    ./.venv/bin/python tools/verify_bundle_consts.py /Applications/实时股价-目标价版.app \
        sf_ui_render BOTTOM_ROW_GAP BOTTOM_SECTION_HIDDEN

输出每个常量是**字面量**（直接赋值，给出值）还是**推导值**（由表达式算出，
列出参与推导的常量名）—— 推导值无法从字节码直接读出结果，但能确认它"不是硬编码"。

实现要点:
    - PYZ 是 zlib 压缩归档，明文 grep 必然命中 0，必须解包。
    - 顶层 `NAME = <字面量>` 编译为「一条字面量加载 + STORE_NAME」；只要 STORE_NAME
      前面不是**紧邻**的单条字面量加载，就说明值来自表达式或别的名字 —— **这正是
      "未被硬编码"的证据**，不要误判为"常量缺失"。
    - Python 3.14 起 0~256 的小整数走 `LOAD_SMALL_INT`（`argval` 就是值），
      **不再是** `LOAD_CONST`。老写法会把 `GAP = 24` 这类硬编码误报成"推导值
      （未被硬编码）"，等于让发版核验器说假话，故两种加载都要认。
    - 字面量可能**恰好是 None**（`FLAG = None`），所以"不是字面量"必须用独立哨兵
      `NOT_LITERAL` 表示，不能再用 None 兼职。
"""
import dis
import marshal
import pathlib
import plistlib
import sys
import tempfile

from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader

# "这一句不是紧跟单条字面量加载"的哨兵（与字面量 None 区分开）
NOT_LITERAL = object()

# 直接把值放在 argval 里的字面量加载指令（3.14 新增 LOAD_SMALL_INT）
_LITERAL_LOADS = frozenset({"LOAD_CONST", "LOAD_SMALL_INT"})
# 会往"参与推导的名字"里记一笔的加载指令
_NAME_LOADS = frozenset({"LOAD_NAME", "LOAD_GLOBAL", "LOAD_FAST"})


def bundle_executable(app):
    """从 Info.plist 读取实际的 CFBundleExecutable。"""
    info = pathlib.Path(app) / "Contents" / "Info.plist"
    try:
        with info.open("rb") as f:
            name = plistlib.load(f).get("CFBundleExecutable")
    except (OSError, plistlib.InvalidFileException, ValueError):
        name = None
    if not name:
        raise SystemExit(f"Info.plist 缺少 CFBundleExecutable: {info}")
    return pathlib.Path(app) / "Contents" / "MacOS" / str(name)


def extract_pyz(exe_path):
    reader = CArchiveReader(str(exe_path))
    # CArchive 的 TOC 键即条目名，内嵌 PYZ 归档的条目名以 "PYZ" 开头（如 PYZ-00.pyz）
    name = next(n for n in reader.toc if n.startswith("PYZ"))
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="pyz_consts_", dir="/tmp"))
    pz = tmp / "PYZ.pyz"
    pz.write_bytes(reader.extract(name))
    return pz


def load_module(z, modname):
    c = z.extract(modname)
    # 不同 PyInstaller 版本返回形态不同：新版先包一层 (类型标志, 数据)
    if isinstance(c, tuple):
        c = c[0]
    # 老版本直接给出 marshal 序列化后的字节码
    if isinstance(c, (bytes, bytearray)):
        c = marshal.loads(bytes(c))
    return c


def top_level_consts(code):
    """反汇编顶层字节码，返回 {常量名: {"value": …, "expr_names": […]}}。

    value 是 NOT_LITERAL 表示该常量不是"紧跟单条字面量加载"的赋值（由表达式算出、
    或值来自别的名字）；此时 expr_names 列出参与推导的名字。
    """
    pairs, literal, pending_names = {}, NOT_LITERAL, []
    for ins in dis.get_instructions(code):
        opname = ins.opname
        if opname in _LITERAL_LOADS:
            literal = ins.argval
            continue
        if opname == "STORE_NAME":
            # Instruction 没有 argname，名字在 argrepr 里
            pairs[ins.argrepr] = {
                "value": literal,
                "expr_names": [] if literal is not NOT_LITERAL else list(pending_names),
            }
            literal, pending_names = NOT_LITERAL, []
            continue
        if opname in _NAME_LOADS:
            pending_names.append(ins.argval)
        # 其它任何指令（BINARY_OP / CALL / LOAD_ATTR / IMPORT_FROM …）都说明
        # 这个 STORE_NAME 前面不是一条干净的 literal 赋值
        literal = NOT_LITERAL
    return pairs


def main():
    if len(sys.argv) < 4:
        raise SystemExit(__doc__)
    app, modname = pathlib.Path(sys.argv[1]), sys.argv[2]
    wanted = sys.argv[3:]

    exe = bundle_executable(app)
    if not exe.exists():
        raise SystemExit(f"执行文件不存在: {exe}")

    z = ZlibArchiveReader(str(extract_pyz(exe)))
    pairs = top_level_consts(load_module(z, modname))

    print(f"== {app.name} · 模块 {modname} 顶层常量 ==")
    for name in wanted:
        info = pairs.get(name)
        if info is None:
            print(f"   {name:<26} <未找到该常量>")
        elif info["value"] is NOT_LITERAL:
            srcs = ", ".join(info["expr_names"]) or "（表达式）"
            print(f"   {name:<26} 推导值 —— 由 {srcs} 算出（未被硬编码）")
        else:
            print(f"   {name:<26} {info['value']}")


if __name__ == "__main__":
    main()
