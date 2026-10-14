#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Python 实现的 ObjC 方法签名闸门（v1.4.57 起，第一道防线）

为什么需要它 —— iMac 实机一枚 SIGSEGV（v1.4.55 / macOS 27.0.1，点 Dock 图标即崩）：
PyObjC 在**建 Python 子类那一刻**就要把每个方法的 ObjC 编码写进 runtime，找不到元数据
时一律退化成"返回值和所有参数都是对象(id)"。`applicationShouldHandleReopen:
hasVisibleWindows:` 的元数据在 AppKit 里，而 `sf_logging._ensure_reopen_class()` 建类时
只 import 了 Foundation —— 于是 `hasVisibleWindows` 这个 BOOL 被当成指针：AppKit 发来
YES(1)，PyObjC 拿 0x1 去调 `-self`，主线程 EXC_BAD_ACCESS at 0x1。
同一份包在两台同版本 macOS 上一台正常一台必崩，差别只是"谁先 import"。

判据（自动，不手写期望编码）：
  real —— 在**全新解释器**里按产品的真实调用顺序把类建出来，读 runtime 里登记的编码；
  auth —— 等所有会用到的框架都 import 完，再按同一个 selector 现造一个对照类，
          读它的编码。这一份就是框架元数据给出的权威答案。
两者不一致 → 该方法的参数转换取决于 import 顺序 → 红。今天已知的两处：
  reopen 的 BOOL（靠 sf_logging._pin_reopen_signature 钉死）
  UN delegate 的 block 参数 @?（sf_notify 走 _ensure_un 之后才建类，天然热序）

用法：
  ./.venv/bin/python tools/pyobjc_sig_check.py            # 核对，红=1，工具自身出问题=2
  ./.venv/bin/python tools/pyobjc_sig_check.py --report    # 连未变化的条目一起打印
  ./.venv/bin/python tools/pyobjc_sig_check.py --self-test # 变异自检：种的错必须被抓到

刻意不做的事：不代替用例。它只回答"这个类的 marshalling 会不会随 import 顺序变脸"，
崩溃本身由 tests/test_reopen_signature.py 用 ctypes 复现并守着。
"""

import ast
import json
import os
import pathlib
import subprocess
import sys

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
TIMEOUT_SECONDS = 90
sys.path.insert(0, str(PROJECT_ROOT))     # 子进程从 tools/ 启动，产品模块在工程根

# 产品运行时会碰到的框架：auth 一侧必须把它们全导进来，否则对照类的编码也不权威。
FRAMEWORKS = ("Foundation", "AppKit", "Quartz", "UserNotifications",
              "AVFoundation", "CoreMedia", "CoreAudio")

# 同一座桥的另一半：产品代码从 pyobjc 里 import 的**名字**必须真的存在。
# 实测事故（v1.4.57 同轮）：`from Foundation import dispatch_async` 在 pyobjc 12
# 里直接 ImportError，而 sf_notify 的 import 是一整句 —— 一个名字取不到，整条
# UN 通知路线被判定"不可用"并静默降级 osascript：横幅还在，但点击横幅恢复窗口、
# on_click 回调全没了，日志里只留一行 install error。这类"升级即失效"的形状，
# 静态闸门不查、运行期又被 try/except 兜住，只能专门盯。
PYOBJC_MODULES = {"Foundation", "AppKit", "Quartz", "UserNotifications",
                  "AVFoundation", "CoreMedia", "CoreAudio", "CoreFoundation",
                  "objc", "Vision"}

# AST 扫出来的每个 ObjC 子类都必须在这里有一条：value 是子进程里的构造函数名。
# 新增 NSObject 子类而没进这张表 = 本闸门直接红（它守的就是"新写的代理类没人看过签名"）。
FACTORIES = {
    "sf_logging._ReopenForwardDelegate": "reopen_delegate",
    "sf_notify._NotifyDelegate": "notify_delegate",
    "sf_system_alert._AlertTarget": "alert_target",
}

# --self-test 的种样例：就是本轮 iMac 崩掉的那一个 selector，故意**不钉签名**建类。
PLANTED_SELECTOR = "applicationShouldHandleReopen:hasVisibleWindows:"


# ---------------------------------------------------------------- 静态部分（父进程）
def _product_files(root):
    """产品范围内的 .py：闸门与用例自己造的探针类不算（它们由 --self-test 负责）。"""
    skip_parts = {".venv", "build", "backups", ".git", "__pycache__", "node_modules"}
    for path in sorted(root.rglob("*.py")):
        rel_parts = path.relative_to(root).parts
        if "archive" in rel_parts or any(p in skip_parts for p in rel_parts):
            continue
        if rel_parts[0] in ("tools", "tests"):
            continue
        yield path, rel_parts


def _parse(path):
    try:
        return ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (SyntaxError, ValueError):
        return None


def scan_objc_classes(root):
    """返回 {module.class: [selector, …]}：只收"有 Python 实现的 ObjC 方法"的类。"""
    found = {}
    for path, _parts in _product_files(root):
        tree = _parse(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            bases = [ast.unparse(b).split(".")[-1] for b in node.bases]
            if not any(b in ("NSObject", "NSResponder", "NSProxy") for b in bases):
                continue
            selectors = []
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name.endswith("_") \
                        and not item.name.startswith("__"):
                    selectors.append(item.name[:-1].replace("_", ":") + ":")
            if selectors:
                module = path.relative_to(root).with_suffix("").as_posix().replace("/", ".")
                found["%s.%s" % (module, node.name)] = selectors
    return found


def completeness_errors(found):
    """表里没登记 / 登记了但源码里已经找不到 —— 两种都算红。"""
    bad = []
    for key, selectors in sorted(found.items()):
        if key not in FACTORIES:
            bad.append("新写的 ObjC 代理类 %s（selector：%s）没进 FACTORIES —— "
                       "它的方法签名从没被核对过" % (key, ", ".join(selectors)))
    for key in sorted(FACTORIES):
        if key not in found:
            bad.append("FACTORIES 里的 %s 在源码里已经不存在 —— 表要同步删" % key)
    return bad


def scan_pyobjc_symbols(root):
    """{(框架模块, 名字): [落点,…]} —— 产品代码从 pyobjc 里 import 的每一个名字。"""
    pairs: dict[tuple, list] = {}
    for path, _parts in _product_files(root):
        tree = _parse(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not node.module:
                continue
            if node.module.split(".")[0] not in PYOBJC_MODULES:
                continue
            for alias in node.names:
                if alias.name == "*":
                    continue
                pairs.setdefault((node.module, alias.name), []).append(
                    "%s:%d" % (path.relative_to(root), node.lineno))
    return pairs


def check_symbols(root):
    """逐个 getattr：取不到的名字就是"这条路线永远走不到"，必须报红。"""
    bad = []
    pairs = scan_pyobjc_symbols(root)
    loaded: dict[str, object] = {}
    for (module, name), where in sorted(pairs.items()):
        if module not in loaded:
            try:
                loaded[module] = __import__(module, fromlist=["__name__"])
            except Exception as exc:
                loaded[module] = exc
        target = loaded[module]
        if isinstance(target, Exception):
            bad.append("%s 在本机导入失败（%s: %r），引用点 %s —— 依赖没装或版本不对"
                       % (module, where[0].split(":")[0], target, ", ".join(where)))
            continue
        if not hasattr(target, name):
            bad.append("%s 没有 %s（%s），引用点 %s —— 该功能会在 import 处抛错，"
                       "被 try/except 兜住后整条路线静默降级"
                       % (module, name, where[0].split(":")[0], ", ".join(where)))
    if not pairs:
        # 零输入不是"全过"（2026-10-12 全方位复核 F5 同族，与 test_discipline_scan 的
        # min_files 红线一个道理）：这一层扫的是全仓每一处 `from <pyobjc 框架> import X`，
        # 一旦大家改用 `import Foundation` + 属性访问，或扫描根目录被挪走，pairs 就归零，
        # 旧实现会安静返回"0 处符号引用全部取得到"并放行 —— 而 pyobjc 12 删掉
        # dispatch_* 那类事故正是靠这一层拦的。
        bad.append("一处 pyobjc 符号引用都没扫到 → 扫描范围失效（导入写法变了？目录挪了？），"
                   "这一层等于没跑，拒绝给结论")
    return bad, len(pairs)


# ---------------------------------------------------------------- 构造部分（子进程）
def _build_reopen_delegate():
    import sf_logging
    cls = sf_logging._ensure_reopen_class()
    return cls


def _build_notify_delegate():
    import sf_notify

    class _StubCenter:
        def setDelegate_(self, delegate):    # 产品只需这一下；不真连通知中心
            pass

    sf_notify._ensure_un()                  # 产品的真实顺序：先备齐 UN 框架再建 delegate
    sf_notify._DELEGATE = None
    sf_notify._install_delegate(_StubCenter())
    if sf_notify._DELEGATE is None:
        return None
    return type(sf_notify._DELEGATE)


def _build_alert_target():
    import sf_system_alert
    return sf_system_alert._AlertTarget


BUILDERS = {
    "reopen_delegate": _build_reopen_delegate,
    "notify_delegate": _build_notify_delegate,
    "alert_target": _build_alert_target,
}


def _encoding(cls, selector):
    """runtime 里真正登记的那份编码 —— 消息转发时用的就是它。"""
    try:
        meta = getattr(cls, "class")()
        sig = meta.instanceMethodSignatureForSelector_(selector)
        if sig is None:
            return None
        out = [bytes(sig.methodReturnType()).decode()]
        for i in range(sig.numberOfArguments()):
            out.append(bytes(sig.getArgumentTypeAtIndex_(i)).decode())
        return out
    except Exception as exc:                    # 读取口自己不许抛：红要可读，不要栈
        sys.stderr.write("encoding read failed for %s/%s: %r\n"
                         % (getattr(cls, "__name__", "?"), selector, exc))
        return None


def _define_control(selector, tag):
    """按同一个 selector 现造一个类（必须在框架全都 import 之后调用）。"""
    import Foundation

    def _impl(self, *args):
        return None

    name = "Probe%s" % tag
    return type(name, (Foundation.NSObject,), {selector.replace(":", "_"): _impl})


def _planted_cold_bad(selector):
    """--self-test 的种样例：不钉签名就建类，必须与权威编码不一致。"""
    from Foundation import NSObject

    def _impl(self, *args):
        return None

    return type("PlantedBad", (NSObject,), {selector.replace(":", "_"): _impl})


def child_real(found_map):
    """真实一侧：在全新解释器里按产品的真实调用顺序把类建出来，读 runtime 登记的编码。"""
    notes = []
    real = {}

    # --plant 必须在**任何产品工厂跑之前**建类：sf_logging 一钉签名，NSObject 的
    # 子类就都能拿到权威编码，种样例会假绿（实测踩过，所以顺序写死在这里）。
    if "--plant" in sys.argv:
        cls = _planted_cold_bad(PLANTED_SELECTOR)
        real["__planted__#%s" % PLANTED_SELECTOR] = _encoding(cls, PLANTED_SELECTOR)

    for key, selectors in sorted(found_map.items()):
        builder = BUILDERS.get(FACTORIES.get(key, ""), None)
        if builder is None:
            continue                      # 由父进程的 completeness_errors 负责报红
        try:
            cls = builder()
        except Exception as exc:
            notes.append("%s 建不出来：%r" % (key, exc))
            continue
        if cls is None:
            notes.append("%s 在本机不可用（框架缺失/降级），本条不计入核对" % key)
            continue
        for sel in selectors:
            enc = _encoding(cls, sel)
            if enc is None:
                notes.append("%s 上读不到 %s 的编码" % (key, sel))
                continue
            real["%s#%s" % (key, sel)] = enc

    print(json.dumps({"real": real, "auth": {}, "notes": notes}, sort_keys=True))
    return 0


def child_auth(selector_json):
    """权威一侧：**绝不 import 任何产品模块**，只导框架，然后按 selector 现造对照类。

    为什么必须分进程：产品侧的钉法是 `registerMetaDataForSelector(b"NSObject", …)`，
    注册在 NSObject 上、对本进程后续建的**所有**子类生效。要是对照类跟产品类住在
    同一个进程里，它读到的就是那份钉好的编码 —— 于是"钉错了值"（比如把 BOOL 写成
    int）也永远比对不出差异，这道闸门会退化成一面镜子。实测过这条：同一进程里
    把 pin 改成 b'i' 照样全绿，分成两个进程才红。
    """
    notes = []
    for name in FRAMEWORKS:
        try:
            __import__(name)
        except Exception as exc:
            notes.append("框架 %s 不可用：%r（该框架声明的 selector 给不出权威编码）"
                         % (name, exc))
    auth = {}
    for tag, sel in enumerate(json.loads(selector_json)):
        cls = _define_control(sel, tag)
        enc = _encoding(cls, sel)
        if enc is None:
            notes.append("%s 取不到权威编码" % sel)
        else:
            auth[sel] = enc
    print(json.dumps({"real": {}, "auth": auth, "notes": notes}, sort_keys=True))
    return 0


def child(found_map):
    """子进程入口：--child real（默认）走产品顺序，--child auth 走框架权威侧。"""
    argv = sys.argv[1:]
    role = argv[argv.index("--child") + 1] if "--child" in argv \
        and argv.index("--child") + 1 < len(argv) else "real"
    if role == "auth":
        idx = argv.index("--selectors") + 1 if "--selectors" in argv else None
        return child_auth(argv[idx] if idx else "[]")
    return child_real(found_map)


# ---------------------------------------------------------------- 比对（父进程）
def run_child(extra_args=()):
    cmd = [sys.executable, str(pathlib.Path(__file__).resolve())] + list(extra_args)
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          timeout=TIMEOUT_SECONDS, cwd=str(PROJECT_ROOT))
    if proc.returncode != 0:
        raise RuntimeError("子进程 rc=%r / %s" % (proc.returncode,
                                                  (proc.stderr or proc.stdout)[-300:]))
    line = [l for l in (proc.stdout or "").splitlines() if l.startswith("{")]
    if not line:
        raise RuntimeError("子进程没输出 JSON：%r" % ((proc.stdout or "")[-300:],))
    return json.loads(line[-1])


def collect(found, plant=False):
    """跑两个子进程：真实一侧按产品顺序建类，权威一侧只见框架、不见产品。

    分成两个进程是判据成立的前提：产品侧的钉法注册在 **NSObject** 上，对本进程
    之后建的每一个子类都生效。要是对照类跟产品类住在同一个进程里，它读到的就是
    那份钉好的编码 —— "钉错了值"（例如把 BOOL 写成 int）永远比对不出差异，
    这道闸门会退化成一面镜子（实测：同进程时把 pin 改成 b'i' 照样全绿）。
    """
    selectors = sorted({s for sels in found.values() for s in sels}
                       | ({PLANTED_SELECTOR} if plant else set()))
    real_payload = run_child(["--child", "real"] + (["--plant"] if plant else []))
    auth_payload = run_child(["--child", "auth", "--selectors", json.dumps(selectors)])
    auth = {}
    for ident, enc in real_payload["real"].items():
        sel = ident.split("#", 1)[1]
        if sel in auth_payload["auth"]:
            auth[ident] = auth_payload["auth"][sel]
    return {"real": real_payload["real"], "auth": auth,
            "notes": (real_payload.get("notes") or []) + (auth_payload.get("notes") or [])}


def compare(payload):
    bad = []
    real, auth = payload["real"], payload["auth"]
    for ident in sorted(real):
        if ident not in auth:
            bad.append("%s：取不到权威编码（框架元数据缺失），本条无法判定" % ident)
        elif real[ident] != auth[ident]:
            bad.append("%s：产品真实顺序登记的编码是 %s，框架元数据要求 %s —— "
                       "参数转换会随 import 顺序变脸（iMac 那枚 SIGSEGV 就是这个形状）"
                       % (ident, real[ident], auth[ident]))
    for ident in sorted(auth):
        if ident not in real:
            bad.append("%s：对照类有编码但产品侧没读到，核对没跑全" % ident)
    return bad


def main(argv):
    if "--child" not in argv:
        # 未知参数一律退 2（AGENTS §3.1 那条链上规矩，2026-10-12 全方位复核 F5 补齐）：
        # 这一枚此前把任何参数都当"不存在"，`--slf-test` 会安静地跑成**正式核验**而不是自检，
        # 用例读到 rc=0 就以为"自检通过"。
        # --child 那一支不检查：它是本文件自己拉子进程的内部入口（run_child），
        # 位置参数是角色名与 selector 的 JSON，不是给人敲的命令行。
        unknown = [a for a in argv if a not in ("--self-test", "--report")]
        if unknown:
            print("FAIL 不认识参数：%s\n     可用：--report / --self-test"
                  % " ".join(unknown))
            return 2
    if not os.path.exists(PROJECT_ROOT / "sf_logging.py"):
        print("FAIL 本闸门必须在工程根目录下跑（找不到 sf_logging.py）")
        return 2

    if "--child" in argv:
        return child(scan_objc_classes(PROJECT_ROOT))

    found = scan_objc_classes(PROJECT_ROOT)
    if not found:
        print("FAIL 一个 Python 实现的 ObjC 类都没扫到 —— 扫描范围失效，不给通过")
        return 2
    errors = completeness_errors(found)
    if errors:
        for line in errors:
            print("FAIL", line)
        return 1

    sym_bad, sym_count = check_symbols(PROJECT_ROOT)
    if sym_bad:
        for line in sym_bad:
            print("FAIL", line)
        print("== pyobjc 符号核对未通过：%d 个引用点里 %d 处取不到 =="
              % (sym_count, len(sym_bad)))
        return 1

    if "--self-test" in argv:
        return self_test(found)

    try:
        payload = collect(found)
    except Exception as exc:
        print("FAIL 子进程跑挂：%r" % (exc,))
        return 2
    bad = compare(payload)
    for note in payload.get("notes") or []:
        print("NOTE", note)
    if "--report" in argv:
        for ident in sorted(payload["real"]):
            print("OK   %-70s %s" % (ident, payload["real"][ident]))
    if bad:
        for line in bad:
            print("FAIL", line)
        print("== pyobjc 签名核对未通过 ==")
        return 1
    print("== pyobjc 签名核对通过：%d 条 selector 的真实编码与框架元数据一致，"
          "%d 处符号引用全部取得到 ==" % (len(payload["real"]), sym_count))
    return 0


def self_test(found):
    """变异自检：种的错必须被抓到，否则这条闸门本身就在说假话。"""
    import tempfile
    bad = []

    # 1) 种一处"不钉签名就建类"：必须被判为编码不一致
    try:
        product = collect(found)
        payload = collect(found, plant=True)
    except Exception as exc:
        print("FAIL 子进程跑挂：%r" % (exc,))
        return 2
    planted = [k for k in payload["real"] if k.startswith("__planted__#")]
    if not planted:
        bad.append("种样例没进核对表 → --plant 失效，本自检是空的")
    else:
        hits = [line for line in compare(payload) if "__planted__" in line]
        if not hits:
            bad.append("种了一处不钉签名的类，却没被判为不一致 → 闸门抓不到本次事故的形状")
        product_hits = [line for line in compare(payload) if "__planted__" not in line]
        if product_hits:
            bad.append("产品条目在自检档里也红了（说明真出问题了）：%s"
                       % "; ".join(product_hits[:2]))

    # 2) 产品自己的条目必须一致（不带种样例的那一趟）
    clean = compare(product)
    if clean:
        bad.append("产品条目不一致：%s" % "; ".join(clean[:2]))

    # 3) 新写的 NSObject 子类没进 FACTORIES 必须被拦下
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        (root / "sf_probe_module.py").write_text(
            "from Foundation import NSObject\n\n\n"
            "class _LurkingDelegate(NSObject):\n"
            "    def applicationShouldHandleReopen_hasVisibleWindows_(self, app, flag):\n"
            "        return True\n", encoding="utf-8")
        scanned = scan_objc_classes(root)
        if "sf_probe_module._LurkingDelegate" not in scanned:
            bad.append("AST 没扫到种下的 ObjC 子类 → 完整性守卫失明（扫到 %r）"
                       % (sorted(scanned),))
        elif not completeness_errors(scanned):
            bad.append("扫到了却没报错 → 没登记的新类照样放行")

    # 4) 表格与源码脱节（登记了但源码里没了）必须报错
    stale = completeness_errors({})
    if not any("已经不存在" in line for line in stale):
        bad.append("FACTORIES 里的条目被删干净时没有报红 → 陈旧表无人发现")

    # 5) 种一个 pyobjc 里取不到的名字（sf_notify 的 dispatch_async 就是这一形状）
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        (root / "sf_probe_symbol.py").write_text(
            "from Foundation import NSObject, dispatch_async_xxx_not_a_symbol\n\n"
            "def f():\n    return NSObject, dispatch_async_xxx_not_a_symbol\n",
            encoding="utf-8")
        sym_bad, sym_total = check_symbols(root)
        if sym_total != 2:
            bad.append("种了 2 个符号引用，扫描只看到 %d 个 → 符号扫描范围失真" % sym_total)
        if not any("dispatch_async_xxx_not_a_symbol" in line for line in sym_bad):
            bad.append("种了不存在的 pyobjc 符号却没报红 → 整条 UN 路线那种静默降级拦不住")

    # 6) 扫描范围失效：一处 pyobjc 引用都扫不到时必须拒绝给结论（零输入 ≠ 全过）
    with tempfile.TemporaryDirectory() as td:
        empty_bad, empty_total = check_symbols(pathlib.Path(td))
        if empty_total or not empty_bad:
            bad.append("空目录扫出 %d 处引用、报红 %d 条 → 零输入被当成通过，"
                       "这一层失明时没人会知道" % (empty_total, len(empty_bad)))

    if bad:
        for line in bad:
            print("FAIL", line)
        return 1
    real_count = len(product["real"])
    _ignored, product_symbols = check_symbols(PROJECT_ROOT)
    print("self-test 通过：不钉签名被抓到、不存在的 pyobjc 符号被抓到、产品 %d 条 selector "
          "编码一致、%d 处符号引用全部取得到、未登记新类被抓到、陈旧条目被抓到"
          % (real_count, product_symbols))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
