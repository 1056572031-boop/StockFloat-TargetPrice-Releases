#!/usr/bin/env python3
"""解包已打包 .app 的字节码，核验**本次**改动是否真的进了产物。

用法:
    ./.venv/bin/python tools/verify_dmg_pyz.py <app路径> \\
        [--check 模块::符号]... [--forbid 模块::符号]... [--version X.Y.Z]

    ./.venv/bin/python tools/verify_dmg_pyz.py /Applications/实时股价-目标价版.app \\
        --check sf_ui_render::BOTTOM_INNER_PATH --check stock_float_window::_repaint_now \\
        --forbid sf_ui_render::BOTTOM_GROUP_GAP

三类核验，职责分清：

1. **永远会跑的结构自检**：Info.plist 能读、可执行文件在、PYZ 解得开、
   入口脚本与核心模块都能反序列化，且 `sf_constants.APP_VERSION` 与
   Info.plist 的 `CFBundleShortVersionString` 同源。
2. **本轮特征（--check / --forbid）**：`模块::符号` 逐条断言"这个名字出现在包内
   字节码里"（或 --forbid 断言**不再出现**，用于"某函数已删除"这类验收）。
   符号判定走 `co_names`（含嵌套代码对象）—— 模块级赋值、方法名、被调用的名字
   都会进 `co_names`，因此不需要（也不允许）去 grep 归档明文。

   ⚠ 但 **字符串常量不在 co_names 里**：像 `feature_enabled("system_drag")` 这种
   只有字面量出现的开关名，用 `--check 模块::system_drag` 必然报"命中 0"（v1.4.29
   实测踩过，差点误判成"开关没进包"）。这类要改成查它的使用点符号（如 `_sys_drag`），
   或直接遍历包内 code 的 co_consts。
3. **--version**：与指定版本号三方比对（命令行 / Info.plist / 包内常量）。

为什么不再有写死的 A/B/C 特征（v1.2.63 的 price_pct_layout / PRICE_ROW_RIGHT_PAD /
PCT_GAP）：那些是十几个版本前的修复特征，早已稳定在包内，**每次发版都无脑 PASS**，
结论文案还打印"DMG 内含 v1.2.63 修复代码"——一个每次都通过的检查等于没有检查，
它给的是超出自身能力的安心感。本轮特征必须由发版人显式传入；一条都没传时，
本工具会**如实降级结论**（"未核验本次修复"）而不是宣布全部通过。
"""
import argparse
import dis
import marshal
import pathlib
import plistlib
import shutil
import sys
import tempfile
from collections import Counter

from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader

CORE_MODULES = ("sf_constants", "sf_ui_render", "renderer", "sf_system_alert")


def read_info_plist(app):
    """读 Contents/Info.plist；读不动就直接拒绝执行（不返回半成品）。"""
    info = pathlib.Path(app) / "Contents" / "Info.plist"
    try:
        with info.open("rb") as f:
            return plistlib.load(f)
    except (OSError, plistlib.InvalidFileException, ValueError):
        raise SystemExit(f"Info.plist 读取失败: {info}")


def bundle_executable(app):
    """从 Info.plist 读取实际的 CFBundleExecutable（与 verify_bundle_consts 同形）。"""
    name = read_info_plist(app).get("CFBundleExecutable")
    if not name:
        raise SystemExit(f"Info.plist 缺少 CFBundleExecutable: "
                         f"{pathlib.Path(app) / 'Contents' / 'Info.plist'}")
    return pathlib.Path(app) / "Contents" / "MacOS" / str(name)


class Bundle:
    """一个 .app 的字节码视图：入口脚本在 CArchive，其余模块在嵌套 PYZ。"""

    def __init__(self, app):
        self.path = pathlib.Path(app)
        self.info_plist = read_info_plist(self.path)
        exe = bundle_executable(self.path)
        if not exe.exists():
            raise SystemExit(f"执行文件不存在: {exe}")
        self.name = self.info_plist.get("CFBundleName") or self.path.name
        self.version = self.info_plist.get("CFBundleShortVersionString")
        self._exe = exe
        self._tmp = pathlib.Path(tempfile.mkdtemp(prefix="pyz_verify_", dir="/tmp"))
        self._ca = CArchiveReader(str(exe))
        pyz_name = next((n for n in self._ca.toc if n.startswith("PYZ")), None)
        if pyz_name is None:
            self.dispose()
            raise SystemExit("未找到 PYZ 归档")
        out = self._tmp / "PYZ-00.pyz"
        out.write_bytes(self._ca.extract(pyz_name))
        self._pz = ZlibArchiveReader(str(out))
        self.pyz_size = out.stat().st_size
        self._cache = {}

    def dispose(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def entries(self):
        """包内可见条目名集合（CArchive + PYZ 合并）。"""
        return set(self._ca.toc) | set(getattr(self._pz, "toc", {}) or {})

    def code(self, modname):
        """取模块的 code 对象；不在包内返回 None（由调用方判 FAIL）。"""
        if modname in self._cache:
            return self._cache[modname]
        raw = None
        for reader in (self._pz, self._ca):
            try:
                raw = reader.extract(modname)
            except KeyError:
                raw = None
            if raw is not None:
                break
        if raw is None:
            return None
        if isinstance(raw, tuple):          # 新版 PyInstaller 先包一层 (类型标志, 数据)
            raw = raw[0]
        if isinstance(raw, (bytes, bytearray)):
            raw = marshal.loads(bytes(raw))
        self._cache[modname] = raw
        return raw


def names(code):
    """递归收集 code 对象里所有全局名/属性名（模块级赋值、def、被调用名都在此）。"""
    out = set()
    stack = [code]
    while stack:
        c = stack.pop()
        out.update(c.co_names)
        for k in c.co_consts:
            if hasattr(k, "co_consts"):
                stack.append(k)
    return out


def symbol_hits(code, symbol):
    """某个名字在字节码里出现的次数（co_names 逐层累计）。"""
    hits, stack = 0, [code]
    while stack:
        c = stack.pop()
        hits += Counter(c.co_names)[symbol]
        for k in c.co_consts:
            if hasattr(k, "co_consts"):
                stack.append(k)
    return hits


def top_level_literal(code, name):
    """读模块顶层 `name = <字面量>` 的字面量值；取不到返回 None。

    Python 3.14 起 0~256 的整数编成 LOAD_SMALL_INT（值在 argval），只认
    LOAD_CONST 会把写死的整数读成"取不到"。
    """
    found, literal = None, None
    for ins in dis.get_instructions(code):
        if ins.opname in ("LOAD_CONST", "LOAD_SMALL_INT"):
            literal = ins.argval
        elif ins.opname == "STORE_NAME":
            if ins.argrepr == name and found is None:
                found = literal
            literal = None
        else:
            # 其它任何指令（LOAD_NAME / BINARY_OP / CALL…）都说明这一次赋值
            # 不是"紧跟一条字面量加载"，不能把上一次的残留当成本常量的值
            literal = None
    return found


def parse_feature(items, flag):
    """把 ["模块::符号", ...] 解析成 [(模块, 符号)]；格式不对直接拒绝执行。"""
    specs = []
    for item in items or ():
        module, sep, symbol = item.partition("::")
        if not sep or not module.strip() or not symbol.strip():
            raise SystemExit(f"{flag} 需要 '模块::符号' 形式，收到: {item!r}")
        specs.append((module.strip(), symbol.strip()))
    return specs


def run_checks(bundle, checks, forbids, want_version):
    """返回 [(标签, 是否通过, 详情)]；标签顺序即打印顺序。"""
    results = []
    entries = bundle.entries()

    # 1) 结构自检：入口脚本 + 核心模块都能反序列化
    for mod in ("stock_float_window",) + CORE_MODULES:
        try:
            loaded = bundle.code(mod) is not None
        except Exception as exc:
            loaded = False
            results.append((f"结构 · 模块 {mod} 可反序列化", False, repr(exc)))
            continue
        results.append((f"结构 · 模块 {mod} 可反序列化", loaded,
                        "在包内" if loaded else "不在包内"))
    # 2) 包内确实有 PYZ（防"空壳产物"）
    results.append(("结构 · PYZ 归档非空", bool(entries), f"条目 {len(entries)} 个"))

    # 3) 版本同源：包内常量 == Info.plist（== 命令行期望值）
    const_ver = None
    code, read_err = _safe_code(bundle, "sf_constants")
    if code is not None:
        const_ver = top_level_literal(code, "APP_VERSION")
    same = bool(bundle.version) and const_ver == bundle.version and read_err is None
    results.append(("版本 · APP_VERSION 与 Info.plist 同源", same,
                    ("sf_constants 读不出来：" + read_err) if read_err
                    else f"代码={const_ver}, Info.plist={bundle.version}"))
    if want_version:
        results.append(("版本 · 与命令行期望版本一致",
                        same and bundle.version == want_version,
                        f"期望={want_version}, 实际={bundle.version}"))

    # 4) 本轮特征：出现 / 不再出现
    for module, symbol in checks:
        code, read_err = _safe_code(bundle, module)
        if code is None:
            results.append((f"特征 · {module}.{symbol} 在包内", False,
                            read_err or "模块不在包内"))
            continue
        hits = symbol_hits(code, symbol)
        results.append((f"特征 · {module}.{symbol} 在包内", hits > 0, f"命中 {hits}"))
    for module, symbol in forbids:
        code, read_err = _safe_code(bundle, module)
        if read_err is not None:
            # 读不出来绝不等于"已经删掉了"：旧实现这里给 True，等于给了一条
            # 只要产物损坏就能无脑 PASS 的路（G01 的另一种变体）
            results.append((f"清除 · {module}.{symbol} 已不在包内", False,
                            "模块读不出来，无法判定：" + read_err))
            continue
        if code is None:
            results.append((f"清除 · {module}.{symbol} 已不在包内", True, "模块不在包内"))
            continue
        hits = symbol_hits(code, symbol)
        results.append((f"清除 · {module}.{symbol} 已不在包内", hits == 0,
                        f"命中 {hits}（应为 0）"))
    return results


def conclusion(failures, features_given):
    """结论文案必须与实际跑过的检查一致 —— 没传特征就不能说"本次修复已进包"。"""
    if failures:
        return f"存在 {failures} 项 FAIL，需重新打包 =="
    if not features_given:
        return ("产物结构与版本同源通过；**本轮未提供 --check/--forbid 特征，"
                "本工具不能证明本次修复在包内** ==")
    return "结构与版本同源通过，本轮传入的特征全部命中 =="



class _FakeBundle:
    """只给 run_checks 用的假产物：entries/code/version 三个接口。

    为什么用鸭子类型而不是真 .app：自检要证明的是**判定会不会红**，
    这三样就是 run_checks 唯一依赖的面。真产物核验另有一条用例在发版链上跑。
    """

    def __init__(self, modules=None, version="1.4.46", entries=12):
        self._modules = modules or {}
        self.version = version
        self._entries = list(range(entries)) if isinstance(entries, int) else entries

    def entries(self):
        return self._entries

    def code(self, modname):
        item = self._modules.get(modname)
        if isinstance(item, BaseException):
            raise item
        return item

    def dispose(self):
        pass


def _mod(source):
    return compile(source, "<fake>", "exec")


def self_test():
    """变异自检：这条核验器历史上整枚作废过约 54 个版本（台账 G01）。

    当时的事实：写死了 v1.2.63 的一批特征，那些符号早已稳定在包内，于是每次发版
    都无脑 PASS，结论文案还打印"DMG 内含 v1.2.63 修复代码"。所以这里每一类"该红"
    的形状都必须真的红，还要把两条**已知的字节码边界**锁成可执行的事实
    （不是只写在注释里，注释会被下一个人当耳旁风）：
      · 纯字符串字面量不在 co_names 里 —— `--check 模块::system_drag` 必然报 0 命中；
      · 表达式赋值（`DERIVED = GAP + 1`）不能读成字面量 1（台账 G03 就是把 3 读成了 1）。
    """
    bad = []
    # ---- 1) 特征不存在必须 FAIL（旧版本在这条上无脑 PASS）----
    bundle = _FakeBundle(modules={"sf_glass": _mod("def glass_diagnostics(self): return 1")})
    res = dict((label, passed) for label, passed, _ in
               run_checks(bundle, [("sf_glass", "glass_diagnostics")], [], None))
    if not any(passed for label, passed in res.items() if label.startswith("特征")):
        bad.append("包内真有的符号被判成未命中（会把正常发版拦死）")
    res2 = run_checks(bundle, [("sf_glass", "never_exists")], [], None)
    if all(passed for _l, passed, _d in res2 if _l.startswith("特征")):
        bad.append("--check 一个不存在的符号竟然全 PASS —— G01 就是这么废的")
    # ---- 2) 模块不在包内也必须 FAIL，不能"看不见就算过"----
    res3 = run_checks(_FakeBundle(modules={}), [("sf_ghost", "anything")], [], None)
    if any(passed for _l, passed, _d in res3 if _l.startswith("特征")):
        bad.append("模块不在包内却被判 PASS")
    # ---- 3) forbid：符号还在就必须 FAIL；真删掉了才 PASS----
    res4 = run_checks(bundle, [], [("sf_glass", "glass_diagnostics")], None)
    if any(passed for _l, passed, _d in res4 if _l.startswith("清除")):
        bad.append("--forbid 的符号明明还在却被判「已清除」")
    res5 = run_checks(bundle, [], [("sf_glass", "gone_symbol")], None)
    if not all(passed for _l, passed, _d in res5 if _l.startswith("清除")):
        bad.append("确实不存在的符号被 --forbid 判成失败")
    # ---- 4) 空 PYZ（空壳产物）必须 FAIL ----
    empty = _FakeBundle(modules={"sf_constants": _mod('APP_VERSION = "1.4.46"')}, entries=[])
    if all(passed for _l, passed, _d in run_checks(empty, [], [], None)):
        bad.append("PYZ 一个条目都没有（空壳产物）却没判红")
    # ---- 5) 版本三方比对：任一不符必须 FAIL ----
    ok_bundle = _FakeBundle(modules={"sf_constants": _mod('APP_VERSION = "1.4.46"')})
    if not all(passed for _l, passed, _d in run_checks(ok_bundle, [], [], "1.4.46")
               if _l.startswith("版本")):
        bad.append("版本同源却被判 FAIL")
    old_bundle = _FakeBundle(modules={"sf_constants": _mod('APP_VERSION = "1.4.44"')},
                             version="1.4.46")
    if all(passed for _l, passed, _d in run_checks(old_bundle, [], [], None)
           if _l.startswith("版本")):
        bad.append("包内常量与 Info.plist 不一致（代码旧、标签新）却没判红")
    mism = _FakeBundle(modules={"sf_constants": _mod('APP_VERSION = "1.4.46"')}, version="1.4.46")
    if all(passed for _l, passed, _d in run_checks(mism, [], [], "9.9.9")
           if _l.startswith("版本")):
        bad.append("命令行期望版本与实际不符却没判红")
    # ---- 6) 反序列化抛异常必须 FAIL，不能被 except 咽成"没结论"----
    # 故意把 sf_glass 也做成"读不出来"，好让 --forbid 那条路被真的走到
    # （否则假包里根本没有这个模块，会落进"模块不在包内"那条旧语义，测不到新判据）
    boom = _FakeBundle(modules={m: RuntimeError("bad marshal") for m in
                                ("stock_float_window", "sf_glass") + CORE_MODULES})
    if not any(not passed for _l, passed, _d in run_checks(boom, [], [], None)):
        bad.append("模块反序列化炸了却没有任何 FAIL")
    if any(passed for _l, passed, _d in run_checks(boom, [], [("sf_glass", "whatever")], None)
           if _l.startswith("清除")):
        bad.append("模块读不出来却被判「已清除」—— 这是又一条只要损坏就能 PASS 的路")
    if any(passed for _l, passed, _d in run_checks(boom, [], [], "1.4.46")
           if _l.startswith("版本")):
        bad.append("sf_constants 读不出来却被判版本同源通过")
    # ---- 7) 没传特征时，结论不许宣布"本轮特征全部命中"----
    if "不能证明" not in conclusion(0, False):
        bad.append("未传特征却给出无保留结论 —— 正是那条永远过的关的文案")
    if "不能证明" in conclusion(0, True) or "FAIL" not in conclusion(2, True):
        bad.append("结论与实跑检查不符")
    # ---- 8) 伪特征必须在解析期就被拒（台账 G02：一条永不可能命中的特征）----
    for wrong in ("glass_diagnostics", "sf_glass::", "::sf_glass", "   ::x"):
        try:
            parse_feature([wrong], "--check")
            bad.append("非法特征 %r 被安静接受" % wrong)
        except SystemExit:
            pass
    # ---- 9) 字节码边界：纯字符串字面量查不到（v1.4.29 踩过的坑）----
    code = _mod('def f():\n    return feature_enabled("system_drag")')
    if symbol_hits(code, "system_drag") != 0:
        bad.append("字符串字面量居然在 co_names 里 —— 边界说明过期了，请同步文档与 release_checks")
    if symbol_hits(code, "feature_enabled") == 0:
        bad.append("使用点符号查不到（说明 names/symbol_hits 漏了嵌套 code）")
    # ---- 10) 顶层字面量：小整数要读得到，表达式不许当成字面量 ----
    if top_level_literal(_mod("SMALL = 3"), "SMALL") != 3:
        bad.append("Py3.14 的 LOAD_SMALL_INT 又读不出来了（C-01 那类 bug 复发）")
    if top_level_literal(_mod('V = "1.4.46"'), "V") != "1.4.46":
        bad.append("字符串常量读不出来")
    derived = top_level_literal(_mod("A = 2\nDERIVED = A + 1\n"), "DERIVED")
    if derived == 1:
        bad.append("把 DERIVED = A + 1 读成了 1 —— G03 那个『中间指令作废字面量』的错复发")
    if top_level_literal(_mod("OTHER = 7\n"), "MISSING") is not None:
        bad.append("查不到的常量返回了值（调用方会把 None 当合法值比下去）")
    for line in bad:
        print("FAIL", line)
    if bad:
        return 1
    print("self-test 通过：11 类形状各判对（不存在的特征/缺模块/forbid 还在/空 PYZ/"
          "版本两两不符/反序列化炸/没传特征的降级/伪特征解析期拒绝/字面量边界/小整数与表达式）")
    return 0

def _safe_code(bundle, modname):
    """取模块 code；读不出来时返回 (None, 错误文字) 而不是把核验器炸掉。

    为什么必须包这一层：自检第一次跑就抓到 `run_checks` 在结构循环之外裸调
    `bundle.code("sf_constants")` —— 产物里那个模块坏掉时整个核验器带着 traceback 退出。
    崩溃和 FAIL 的结果都是"不通过"，但崩溃**不告诉你哪一项坏了**，人会当成
    "工具又抽了"重跑一遍，真故障就被这次重跑掩盖（台账 D-09 那条"离屏段错误
    不可捕获"是同族：失败的可观测形式必须是可读结论）。
    """
    try:
        return bundle.code(modname), None
    except Exception as exc:
        return None, repr(exc)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="解包 .app 字节码，核验本次改动是否真的进了产物")
    parser.add_argument("app", nargs="?",
                        help="待核验的 .app 路径（dist/、/Applications 或挂载卷内）")
    parser.add_argument("--self-test", action="store_true",
                        help="变异自检：证明这条核验器遇到坏产物真的会红（不需要 .app）")
    parser.add_argument("--check", action="append", metavar="模块::符号",
                        help="断言该名字出现在包内字节码里（可重复）")
    parser.add_argument("--forbid", action="append", metavar="模块::符号",
                        help="断言该名字**不再**出现在包内（可重复）")
    parser.add_argument("--version", help="期望版本号，与 Info.plist 及包内常量三方比对")
    args = parser.parse_args(argv)
    if args.self_test:
        return self_test()
    if not args.app:
        parser.error("需要一个 .app 路径，或者用 --self-test 跑变异自检")

    checks = parse_feature(args.check, "--check")
    forbids = parse_feature(args.forbid, "--forbid")

    bundle = Bundle(args.app)
    try:
        results = run_checks(bundle, checks, forbids, args.version)
    finally:
        bundle.dispose()

    print(f"== 产物字节码核验: {bundle.name} ==")
    print(f"   路径: {bundle.path}")
    print(f"   PYZ 大小: {bundle.pyz_size / 1024 / 1024:.2f} MB")
    print(f"   本轮特征: --check {len(checks)} 条 / --forbid {len(forbids)} 条")
    print()
    failures = sum(1 for _, passed, _ in results if not passed)
    for label, passed, detail in results:
        print(f"   [{'PASS' if passed else 'FAIL'}] {label:<44} {detail}")
    print()
    print("== 结论:", conclusion(failures, bool(checks or forbids)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
