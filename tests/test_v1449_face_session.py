# -*- coding: utf-8 -*-
"""PyObjC 元组返回形状的回归用例（v1.4.49，台账 E02）。

用户报「第二个人脸出现时主窗没收起」。实机日志的真相是：采集会话**从来没起来过** ——
`face_guard session FAILED: ValueError('NSInvalidArgumentException -
-[OC_BuiltinPythonArray session]: unrecognized selector ...')`。

根因：`AVCaptureDeviceInput.deviceInputWithDevice_error:` 在装了
pyobjc-framework-AVFoundation 的环境里返回 **`(对象, NSError)` 元组**，
旧代码把整个元组当对象用（还写了 `if not input_port:` —— 元组恒真，那条保护形同虚设），
元组被桥接成 NSArray 交给 `canAddInput_`，AVFoundation 反过来问它要 `-session`。
本机实测复现（同一 .venv、同一台 MacBook Air）：
    返回 tuple (<AVCaptureDeviceInput: ...>, None)
    canAddInput_(元组) -> ValueError('... -[OC_BuiltinPythonArray session]: unrecognized selector ...')

为什么存量 82 条人脸守卫用例一条都没抓到：它们全部用假类，而假类的返回值是**手写的
单个对象** —— 恰好不是 PyObjC 在真实框架下的返回形状。也就是说这套用例把这条路径
整体测不到，还给出"已覆盖"的错觉（防线二失守，不是防线缺失）。
这几条用例把形状本身当被测对象：桩必须**按真实形状**返回元组。
"""
import unittest
import unittest.mock

try:
    from _base import SFCase
except ImportError:
    from tests._base import SFCase

import sf_presence as sp


class _Port:
    """假的 AVCaptureDeviceInput：只有被传对了对象才算成功。"""


class _Session:
    def __init__(self, log):
        self._log = log

    def canAddInput_(self, port):
        self._log.append(("canAddInput", port))
        # 真实 AVFoundation 收到 NSArray 时的行为：拒收（这里用 isinstance 复现同一判据）
        return not isinstance(port, tuple)

    def addInput_(self, port):
        self._log.append(("addInput", port))

    def canAddOutput_(self, out):
        return True

    def addOutput_(self, out):
        self._log.append(("addOutput", out))

    def startRunning(self):
        self._log.append(("startRunning", None))


class _Cls(dict):
    """最小框架表：只给 _build_session 真正用到的那几个键。"""

    def __init__(self, input_result, log):
        super().__init__()
        port = input_result

        class _InputFactory:
            @staticmethod
            def deviceInputWithDevice_error_(_device, _err):
                return port

        class _OutputFactory:
            @staticmethod
            def alloc():
                out = _Obj()
                return out

        class _SessionFactory:
            @staticmethod
            def alloc():
                return _Obj(log)

        self["AVCaptureDeviceInput"] = _InputFactory
        self["AVCaptureVideoDataOutput"] = _OutputFactory
        self["AVCaptureSession"] = _SessionFactory
        self["NSObject"] = object


class _Obj:
    def __init__(self, log=None):
        self._log = log
        self._calls = []

    def init(self):
        return self

    def setAlwaysDiscardsLateVideoFrames_(self, flag):
        self._calls.append(("discard", flag))

    def setSampleBufferDelegate_queue_(self, sink, queue):
        self._calls.append(("delegate", sink))

    # 会话侧
    def canAddInput_(self, port):
        return _Session(self._log).canAddInput_(port)

    def addInput_(self, port):
        return _Session(self._log).addInput_(port)

    def canAddOutput_(self, out):
        return True

    def addOutput_(self, out):
        self._log.append(("addOutput", out))

    def startRunning(self):
        self._log.append(("startRunning", None))


class FaceSessionShapeCase(SFCase):
    """_build_session 必须吃下 PyObjC 的元组返回形状。"""

    def _monitor(self):
        mon = sp.FacePresenceMonitor()
        # 委托与 GCD 队列不是本用例的靶子（它们各自有真链路验证），桩掉避免拉起框架
        mon._make_sink = lambda cls: object()   # discipline-ok(R5): 采集会话的唯一建法没有对外入口，桩掉委托避免拉真框架
        self._q = _dispatch_patcher = None
        return mon

    def _build(self, input_result):
        log = []
        mon = self._monitor()
        with unittest.mock.patch.object(sp, "_dispatch_queue", lambda: "queue-stub"):
            mon._build_session(_Cls(input_result, log), device="dev")   # discipline-ok(R5): 被测目标就是这个内部方法，公开路径要真摄像头
        return log

    def test_tuple_result_is_unpacked_before_touching_the_session(self):
        """真实形状 (对象, None)：传给 canAddInput_ 的必须是对象本体，不是元组。"""
        port = _Port()
        log = self._build((port, None))
        given = [v for name, v in log if name == "canAddInput"]
        self.assertEqual(given, [port],
                         "canAddInput_ 收到的不是输入端口本体（元组会被桥接成 NSArray，"
                         "AVFoundation 会向它发 -session 并抛异常）")
        self.assertIn(("startRunning", None), log, "会话没真正启动 —— 第二张脸永远不会被检出")

    def test_plain_object_still_works(self):
        """没有 AVFoundation 元数据的环境返回单对象：不能因为解包而变脆。"""
        port = _Port()
        log = self._build(port)
        self.assertIn(("canAddInput", port), log)
        self.assertIn(("startRunning", None), log)

    def test_nil_input_raises_with_the_system_reason(self):
        """建立失败时 (None, NSError)：必须抛错并把系统原因带出来，而不是静默继续。"""
        reason = "设备正被其他程序占用"
        with self.assertRaises(RuntimeError) as ctx:
            self._build((None, reason))
        self.assertIn(reason, str(ctx.exception),
                      "报错里要能看见系统给的原因，否则实机只能看到一句空话")

    def test_none_input_without_reason_still_raises(self):
        with self.assertRaises(RuntimeError):
            self._build((None, None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
