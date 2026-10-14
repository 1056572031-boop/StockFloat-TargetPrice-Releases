# -*- coding: utf-8 -*-
"""v1.4.44 人脸守卫委托类的再入判据（复核 F1 / F3）。

守的是一句很具体的用户后果：**关掉再打开、或经历任何一次"停→起"之后，人脸守卫
还能不能再起来**。修复前不能 —— 委托类每次 `start()` 都用同名 `type()` 新建一个，
而 PyObjC 的 ObjC 类注册不可逆，第二次同名注册直接抛
`SFPresenceSink is overriding existing Objective-C class`（本机用同一个 objc 基类
连建两次实测复现）。后果是功能每个进程只能成功启动一次，而主窗按 10 秒节拍重试、
每次刷一行 ERROR —— v1.4.41/42 承诺的"中断后能自愈"就是这样被挡死的。

只用 AppKit 的 NSObject 当基类，不碰摄像头、不建窗口：要验的是"类注册"这件事，
与设备无关，所以刻意不调 `start()`（那需要真设备）。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sf_presence  # noqa: E402
from sf_presence import FacePresenceMonitor  # noqa: E402
from tests._base import SFCase  # noqa: E402

# 被检的就是这些模块级私有成员（进程内唯一的类注册表、委托登记表），没有公开出口
# 可查，所以豁免集中在这一处，用例正文一律用别名。
_sink_class = sf_presence._presence_sink_class      # discipline-ok(R5): 被测对象本身是模块私有实现
_make_sink = FacePresenceMonitor._make_sink          # discipline-ok(R5): 同上
_takedown = FacePresenceMonitor._teardown            # discipline-ok(R5): 同上
_owners = sf_presence._SINK_OWNERS                   # discipline-ok(R5): 反查表就是本轮的判据本体

try:
    import AppKit
    _BASE = AppKit.NSObject
except Exception:                                    # 非 macOS / 无 PyObjC
    AppKit = None
    _BASE = None


class SinkClassRegisteredOnce(SFCase):
    def setUp(self):
        super().setUp()          # SFCase 要在这里登记 patch 与 widget 的清理表
        if _BASE is None:
            self.skipTest("本机没有 PyObjC/AppKit，无法验 ObjC 类注册行为")

    def test_second_request_returns_the_same_class_without_raising(self):
        self.assertIs(_sink_class(_BASE), _sink_class(_BASE), "委托类必须进程内唯一")

    def test_the_old_way_really_was_a_hard_failure(self):
        """把修复前的写法当场跑一遍：同名第二次注册确实抛 —— 这条红线是真的。

        用一次性类名，不去碰 `SFPresenceSink`：那个名字由进程内的缓存持有，
        别的用例先跑还是后跑会影响它的状态，用它做判据就是一条看顺序的脸脆用例
        （第一版就是这么 flaky 过一次）。这里要证的只是 PyObjC 的重注册规则本身。
        """
        name = "SFOverrideProbe_%d" % os.getpid()
        type(name, (_BASE,), {"ping": lambda self: 1})
        with self.assertRaises(Exception) as ctx:
            type(name, (_BASE,), {"ping": lambda self: 1})
        self.assertIn("overriding existing Objective-C class", str(ctx.exception))

    def test_a_second_monitor_can_still_get_a_delegate(self):
        """用户"关了再开"走的就是这条路：第二个 monitor 也拿得到委托实例。"""
        sink_a = _make_sink(FacePresenceMonitor(), {"NSObject": _BASE})
        sink_b = _make_sink(FacePresenceMonitor(), {"NSObject": _BASE})
        self.assertIsNotNone(sink_a)
        self.assertIsNotNone(sink_b, "第二次起委托不许抛 —— 抛了就等于功能永久失效")

    def test_delegate_finds_its_own_monitor_both_ways(self):
        """反查的两条路都要通：实例属性 + 按实例登记的表。

        为什么要两条：由系统侧发起回调时万一拿到的是另取的代理，只靠一条就可能
        "绿灯亮着却一帧都没算过"，那是最难查的失效形态。实测两条都命中（且
        PyObjC 同一实例的 Python 代理 id 稳定、hash 走 ObjC 的 -hash）。
        """
        mon = FacePresenceMonitor()
        sink = _make_sink(mon, {"NSObject": _BASE})
        self.assertIs(getattr(sink, "_owner", None), mon)
        self.assertIs(_owners.get(sink), mon)

    def test_each_delegate_points_at_its_own_monitor(self):
        """两个委托各认各的主人：类只建一次之后不能把帧喂给错的 monitor。"""
        mon_a, mon_b = FacePresenceMonitor(), FacePresenceMonitor()
        sink_a = _make_sink(mon_a, {"NSObject": _BASE})
        sink_b = _make_sink(mon_b, {"NSObject": _BASE})
        self.assertIsNot(sink_a, sink_b)
        self.assertIs(_owners.get(sink_a), mon_a)
        self.assertIs(_owners.get(sink_b), mon_b)

    def test_teardown_unregisters_the_lookup_table(self):
        """拆卸跟着销账：否则每个历史 monitor 都永久挂在进程上（复核 F3 的资源累积）。"""
        mon = FacePresenceMonitor()
        sink = _make_sink(mon, {"NSObject": _BASE})
        self.assertIn(sink, _owners)
        _takedown(mon)
        self.assertNotIn(sink, _owners, "登记表必须随拆卸清空，不然就是新的累积")


if __name__ == "__main__":
    unittest.main()
