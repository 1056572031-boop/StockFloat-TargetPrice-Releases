# -*- coding: utf-8 -*-
"""子进程探针：按 AppKit 派发 reopen Apple Event 的真实调用形态，给 reopen delegate
发 `applicationShouldHandleReopen:hasVisibleWindows:`，第三个参数走 BOOL(YES=1)。

    ./.venv/bin/python tests/_reopen_child.py cold_bad     # 事故形态：不钉签名就建类
    ./.venv/bin/python tests/_reopen_child.py product      # 产品形态：sf_logging 的代理类

两档都会在**建完被测类之后**再 import AppKit 并现造一个对照类，对照类的编码就是
框架元数据给出的权威答案（v@:@B）—— 父用例据此判"钉过的签名与权威签名是否一致"。

为什么走 ctypes 而不是真点 Dock 图标：崩溃发生在 PyObjC 的**参数转换**这一步
（method_stub → pythonify_c_value → objc_opt_self），只要按调用方的真实签名
`v@:@B` 把 1 送进第三个参数槽就能复现，不需要 Apple Event、不需要窗口服务器，
因此这条探针能在 offscreen 的全量回归里跑。实测崩溃报告顶帧完全一致：
  objc_opt_self ← pythonify_c_value ← method_stub ← ffi_closure_SYSV
  EXC_BAD_ACCESS / KERN_INVALID_ADDRESS at 0x0000000000000001

输出标记（父用例按 KEY=VALUE 解析）：
  MODE / ENC_PRODUCT / ENC_HOTNATURAL / PRE_SEND / SURVIVED
cold_bad 这一档预期**不会**打印 SURVIVED —— 子进程直接 SIGSEGV（rc=-11），
那正是"探针能抓到这类崩溃"的证据。
"""
import ctypes
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

MODE = sys.argv[1] if len(sys.argv) > 1 else "product"
SEL = "applicationShouldHandleReopen:hasVisibleWindows:"

import objc                              # noqa: E402  pyobjc-core，不带任何框架元数据
from Foundation import NSObject          # noqa: E402  只导 Foundation —— AppKit 还没就位


def _enc(cls):
    """PyObjC 真正注册进 ObjC runtime 的编码（消息转发时用的就是这一份）。"""
    sig = getattr(cls, "class")().instanceMethodSignatureForSelector_(SEL)
    if sig is None:
        return None
    return [bytes(sig.methodReturnType()).decode()] + [
        bytes(sig.getArgumentTypeAtIndex_(i)).decode() for i in range(sig.numberOfArguments())]


print("MODE=%s" % MODE, flush=True)

if MODE == "cold_bad":
    class _ColdBad(NSObject):            # 事故形态：冷序建类、不钉签名
        def applicationShouldHandleReopen_hasVisibleWindows_(self, app, flag):
            return True
    target = _ColdBad
    enc = _enc(_ColdBad)
else:
    import sf_logging                    # noqa: E402  _ensure_reopen_class 内部先钉签名
    target = sf_logging._ensure_reopen_class()
    enc = sf_logging._reopen_signature(target)
    if enc is not None:
        enc = [enc[0].decode()] + [t.decode() for t in enc[1]]
print("ENC_PRODUCT=%s" % json.dumps(enc), flush=True)

import AppKit                            # noqa: E402  框架元数据就位后现造对照类

class _HotNatural(NSObject):
    def applicationShouldHandleReopen_hasVisibleWindows_(self, app, flag):
        return True

print("ENC_HOTNATURAL=%s" % json.dumps(_enc(_HotNatural)), flush=True)

libobjc = ctypes.CDLL("/usr/lib/libobjc.dylib")
libobjc.sel_registerName.restype = ctypes.c_void_p
libobjc.sel_registerName.argtypes = [ctypes.c_char_p]
libobjc.object_getClass.restype = ctypes.c_void_p
libobjc.object_getClass.argtypes = [ctypes.c_void_p]
libobjc.class_getMethodImplementation.restype = ctypes.c_void_p
libobjc.class_getMethodImplementation.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

proxy = target.alloc().init()
sender = NSObject.new()                  # sender 在两种编码下都是合法对象，不是故障点

self_ptr = ctypes.c_void_p(objc.pyobjc_id(proxy))
app_ptr = ctypes.c_void_p(objc.pyobjc_id(sender))
sel_ptr = libobjc.sel_registerName(SEL.encode("ascii"))
imp_ptr = libobjc.class_getMethodImplementation(libobjc.object_getClass(self_ptr), sel_ptr)
if not imp_ptr:
    print("NO_IMP=1", flush=True)
    sys.exit(3)

# 调用方（AppKit）视角的真实签名 v@:@B：返回值是 BOOL，第三个参数也是 BOOL 而不是对象
imp = ctypes.cast(imp_ptr, ctypes.CFUNCTYPE(
    ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_bool))
print("PRE_SEND=1", flush=True)
print("SURVIVED=%r" % (imp(self_ptr, sel_ptr, app_ptr, True),), flush=True)
