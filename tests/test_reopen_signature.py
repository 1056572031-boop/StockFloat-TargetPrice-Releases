# -*- coding: utf-8 -*-
"""点 Dock 图标（reopen）不能再把程序崩掉 —— iMac 实机事故的可失败复现。

根因（本机实测，非推测）：PyObjC 在**建 Python 子类那一刻**就要确定每个方法的 ObjC
编码，找不到元数据时一律退化成"返回值和所有参数都是对象(id)"。
`applicationShouldHandleReopen:hasVisibleWindows:` 的元数据在 AppKit 里，而
`sf_logging._ensure_reopen_class()` 只 import Foundation —— 于是不等元数据就位就建类，
`hasVisibleWindows` 这个 BOOL 就被当成指针：AppKit 发来 YES(1)，PyObjC 拿 0x1 去调
`-self`，主线程 EXC_BAD_ACCESS / KERN_INVALID_ADDRESS at 0x1，顶帧是
objc_opt_self ← pythonify_c_value ← method_stub ← ffi_closure_SYSV
（与 iMac 崩溃报告逐帧一致，本机探针复现后拿 DiagnosticReports 对过）。

本文件两条子进程档：
  cold_bad —— 不钉签名就建类：必须 rc=-11（证明探针抓得到这类崩溃，不是永真门）
  product  —— 产品的代理类：编码必须等于框架元数据给的权威编码，且调用存活

为什么必须走子进程：签名由"建类时 sys.modules 里有没有 AppKit"决定，测试父进程里
别的用例早就 import 过 AppKit，冷序在父进程复现不了；也只有子进程能把"崩"变成
可观测的退出码。
"""

import json
import os
import pathlib
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sf_logging                                        # noqa: E402

try:
    from _base import SFCase
except ImportError:                                      # pragma: no cover
    from tests._base import SFCase

ROOT = pathlib.Path(__file__).resolve().parent.parent
CHILD = ROOT / "tests" / "_reopen_child.py"
TIMEOUT_SECONDS = 120

# 权威签名不由本文件手写：子进程在"等 AppKit 就位后现造的对照类"上读出
# v@:@B（返回值 BOOL，参数 self@/_cmd:/发送者@/hasVisibleWindows BOOL），
# 父用例比对的就是这一份。


def _pyobjc_ready():
    try:
        import objc          # noqa: F401
        import Foundation    # noqa: F401
        return True
    except Exception:
        return False


def _spawn(mode):
    proc = subprocess.run([sys.executable, str(CHILD), mode],
                          capture_output=True, text=True, timeout=TIMEOUT_SECONDS,
                          cwd=str(ROOT))
    markers = {}
    for line in (proc.stdout or "").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            markers[key.strip()] = value.strip()
    return proc, markers


class ReopenSignatureTest(SFCase):

    def setUp(self):
        super().setUp()
        if not _pyobjc_ready():
            self.skipTest("本机无 pyobjc（冷序/热序都无从判定）")

    def test_unpinned_signature_still_crashes_the_main_thread(self):
        """种一处"不钉签名就建类"，探针必须当场崩掉（rc=-11）。

        这条是整个文件的判据可信度：探针抓不到崩溃时，下面那条 product 就是永真门。
        """
        proc, mk = _spawn("cold_bad")
        self.assertEqual(mk.get("PRE_SEND"), "1",
                         "子进程没能走到发消息这一步：" + (proc.stderr or "")[-300:])
        self.assertEqual(proc.returncode, -11,
                         "不钉签名的 BOOL 参数必须把进程搞崩，实际 rc=%r 输出=%s"
                         % (proc.returncode, (proc.stdout or "")[-300:]))
        self.assertNotIn("SURVIVED", mk, "崩了就不该打印存活标记")
        cold = json.loads(mk["ENC_PRODUCT"])
        hot = json.loads(mk["ENC_HOTNATURAL"])
        self.assertEqual(cold[-1], "@", "冷序建类应退化成'参数是对象'，实测 %r" % (cold,))
        self.assertNotEqual(cold, hot,
                            "冷序与权威签名必须不同（否则本用例守的是不存在的事）")

    def test_pinned_signature_matches_the_framework_and_survives(self):
        proc, mk = _spawn("product")
        self.assertEqual(proc.returncode, 0,
                         "产品形态点 Dock 不该崩：rc=%r / %s / %s"
                         % (proc.returncode, (proc.stdout or "")[-300:],
                            (proc.stderr or "")[-300:]))
        self.assertEqual(mk.get("SURVIVED"), "True",
                         "回调必须按 BOOL 返回 True，让 AppKit 继续走默认恢复行为")
        pinned = json.loads(mk["ENC_PRODUCT"])
        authoritative = json.loads(mk["ENC_HOTNATURAL"])
        self.assertEqual(pinned, authoritative,
                         "钉过的签名与框架元数据的权威签名不一致：%r vs %r"
                         % (pinned, authoritative))
        self.assertTrue(pinned[0] in ("B", "c"),
                        "返回值必须是 BOOL（实测 %r），否则 AppKit 读到的是指针低字节"
                        % (pinned[0],))
        self.assertTrue(pinned[-1] in ("B", "c"),
                        "hasVisibleWindows 必须是 BOOL（实测 %r）" % (pinned[-1],))

    def test_delegate_class_carries_the_pinned_signature(self):
        """产品工厂造出来的类，注册编码就是权威那一份。

        读私有成员的原因：签名是 PyObjC 建类时写进 runtime 的，类本身没有公开读取口；
        冷序只在子进程可复现，这里验的是"本进程拿到的类"这另一半。
        """
        cls = sf_logging._ensure_reopen_class()      # discipline-ok(R5): 没有公开入口，这是本轮事故的落点
        if cls is None:
            self.skipTest("本机建不出 ObjC 代理类")
        sig = sf_logging._reopen_signature(cls)      # discipline-ok(R5): 同上，闸门与探针共用这一个读取口
        self.assertIsNotNone(sig, "读不到注册编码 = 闸门失明")
        retval, args = sig
        self.assertEqual(retval, b"B", "reopen 回调返回值应为 BOOL，实测 %r" % (retval,))
        self.assertEqual(args[-1], b"B",
                         "hasVisibleWindows 应为 BOOL，实测整套编码 %r —— "
                         "退回 '@' 就是 iMac 那枚 SIGSEGV" % (args,))

    def test_pin_failure_leaves_a_trace(self):
        """钉不上签名 = 崩溃窗口重新打开，必须留痕而不是静默降级。"""
        with mock.patch.dict(sys.modules, {"objc": None}), \
                mock.patch.object(sf_logging, "log_swallow") as swallow, \
                mock.patch.object(sf_logging, "log_line") as line:
            self.assertFalse(sf_logging._pin_reopen_signature(),   # discipline-ok(R5): 失败路径只能从内部入口验
                             "objc 不可用时必须报'没钉上'，不能谎报成功")
        self.assertEqual(swallow.call_count, 1, "静默失效红线（§6 第 9 条）：必须走 log_swallow")
        self.assertTrue(line.call_count >= 1, "还要一行可 grep 的 ERROR，否则日志里查不到这枚事故")

    def test_signature_reader_survives_a_class_without_the_selector(self):
        """闸门读取口不能因为一个陌生类就抛 —— 它自己红掉会把整道防线变成噪音。"""
        from Foundation import NSObject

        class _NoReopen(NSObject):
            def justSomethingElse_(self, sender):
                return True

        self.assertIsNone(sf_logging._reopen_signature(_NoReopen),   # discipline-ok(R5): 同上
                          "没有这个 selector 时返回 None，不抛异常")


if __name__ == "__main__":
    unittest.main()
