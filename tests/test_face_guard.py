# -*- coding: utf-8 -*-
"""v1.4.40 人脸在场守卫：迟滞判定、计数口径、降级可见、隐私与生命周期收口。

为什么这几条值得占用例（而不是靠"多用几个用例堆安全感"）：
  · 迟滞判定错了 → 用户反复看到行情自己消失、或该消失时不消失，这是功能的全部价值；
  · 降级不可见 → 用户带着"以为有保护"的心态继续用，比没有这个功能更危险；
  · 帧被缓存/落盘 → 隐私功能反过来变成隐私问题；
  · 退出或手动收起不停摄像头 → 绿灯一直亮，用户无法判断程序是否真的退了。
采集那一段（真起 AVCaptureSession）不在这里测：它需要摄像头硬件与系统授权，
由发版后的实机验证负责。

书写纪律口径（AGENTS §6）：状态一律读 `win.face_guard_diagnostics()` 这个公开只读口，
开关一律通过右键菜单的 QAction.trigger() 走用户真实路径；剩下少数几处必须看内部
状态的，逐行写了 discipline-ok 理由。
"""

import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtCore import QPoint
from PyQt6.QtGui import QContextMenuEvent
from PyQt6.QtWidgets import QApplication

try:
    from _base import SFCase                       # noqa: E402
except ImportError:
    from tests._base import SFCase                 # noqa: E402

import sf_config
import sf_constants
import sf_presence
import sf_quote
import stock_float_window as m
from sf_presence import (EVENT_HIDE, EVENT_IDLE, EVENT_NONE, EVENT_RESTORE,
                         FACE_GUARD_CONF_MIN, FacePresenceMonitor,
                         PresenceArbiter, STATE_DENIED, STATE_NO_CAMERA,
                         STATE_RUNNING, STATE_STARTING, STATE_RESTRICTED,
                         STATE_UNSUPPORTED, count_faces)

# 菜单项的**用户可见文案**（v1.4.53 起是「自动隐藏窗口」）。这里刻意写死字面量
# 而不是 import 产品常量：这条用例守的就是"屏幕上那行字叫什么"，改了名字它必须
# 一起红一次，让人确认所有指路的文案（状态行、last_error）有没有同步改过。
GUARD_MENU_TEXT = "自动隐藏窗口"


def _fake_http_get(url, **kw):
    """离线行情桩：恢复窗口的收尾会排一次 _poll，绝不能让它真去联网。"""
    if "/appstock/app/minute/query?" in url:
        return ('{"data":{"sh600519":{"data":{"date":"20260821",'
                '"data":["0930 1510.0 100 1000"]}}}}')
    return 'v_sh600519="1~贵州茅台~600519~1510.0~1490.0~0.1~1.01~0.5";'



def _long_ago(offset=3600.0):
    """一个"远早于现在"的单调时刻，且在**任何开机时长**下都是正值。

    为什么不能直接写 `time.monotonic() - 3600`：macOS 的 monotonic 从开机算起，
    开机不足一小时时它是**负数**，而产品侧的中断判定把 `baseline <= 0` 当成
    "没有基线"直接 return（`stock_float_window._on_face_guard_tick` 第 4874 行），
    于是这批用例只在"机器已经开着超过一小时"时才绿 —— 结果取决于开机时长，
    不取决于代码。2026-10-09 实测：本机 09:44 重启，09:55 同一棵树红 8 条，
    而重启前 07:2x 与 08:1x 两轮全量 0 失败；HEAD 的干净 worktree 同时刻一样红 8 条。
    修法是把"很久以前"表达成正值，而不是改判据或放宽断言。
    """
    return max(1e-6, time.monotonic() - offset)

class _Obs:
    """替掉 VNFaceObservation：只留 confidence()。"""

    def __init__(self, conf):
        self._conf = conf

    def confidence(self):
        return self._conf


class _ObsBox:
    """带几何的检测框替身。

    `count_faces` 只走 `confidence()` 与 `boundingBox()` 两个入口，所以这里按
    PyObjC 真实桥出来的形状返回 `((x, y), (w, h))`（归一化、原点左下）。
    2026-10-12 实测真框是**正方形**（宽恒等于高），这里的默认值也照这个形状给。
    """

    def __init__(self, conf, x=0.30, w=0.30, y=0.25, h=0.30, geo=True):
        self._conf, self._x, self._w = conf, x, w
        self._y, self._h, self._geo = y, h, geo

    def confidence(self):
        return self._conf

    def boundingBox(self):
        if not self._geo:
            raise RuntimeError("读不出几何（桥接形状变了 / 这个框没带框）")
        return ((self._x, self._y), (self._w, self._h))


class TestPresenceArbiter(SFCase):
    """纯决策层：不需要 Qt，也不需要摄像头。"""

    def test_single_hit_does_not_hide(self):
        """一次看到两张脸不许收窗 —— 屏幕里的人像、身后路过都会单次命中。"""
        a = PresenceArbiter()
        self.assertEqual(a.feed(2, now=0.0), EVENT_NONE)
        self.assertFalse(a.hidden_by_guard)

    def test_sustained_hits_hide_exactly_once(self):
        a = PresenceArbiter()
        events = [a.feed(2, now=float(i)) for i in range(4)]
        self.assertEqual(events.count(EVENT_HIDE), 1,
                         "连续命中只应触发一次收起，之后保持已收起状态")
        self.assertTrue(a.hidden_by_guard)

    def test_passer_by_breaks_the_streak(self):
        """2 张 → 0 张 → 2 张 不算「连续」，否则路过一个人就能把窗收掉。"""
        a = PresenceArbiter()
        self.assertEqual(a.feed(2, now=0.0), EVENT_NONE)
        self.assertEqual(a.feed(0, now=1.0), EVENT_NONE)
        self.assertEqual(a.feed(2, now=2.0), EVENT_NONE)
        self.assertFalse(a.hidden_by_guard)

    def test_empty_room_never_restores(self):
        """一张脸都没有时**不能**自动亮出行情：人走开时暴露屏幕最危险。"""
        a = PresenceArbiter()
        a.feed(2, now=0.0)
        self.assertEqual(a.feed(2, now=1.0), EVENT_HIDE)
        for i in range(20):
            self.assertEqual(a.feed(0, now=2.0 + i), EVENT_NONE,
                             "空房间持续 20 拍也不能触发恢复")
        self.assertTrue(a.hidden_by_guard)

    def test_restore_needs_sustained_single_face(self):
        a = PresenceArbiter()
        a.feed(2, now=0.0)
        self.assertEqual(a.feed(2, now=1.0), EVENT_HIDE)
        events = [a.feed(1, now=2.0 + i) for i in range(a.restore_hits)]
        self.assertEqual(events.count(EVENT_RESTORE), 1,
                         "只剩一张脸要连续满 restore_hits 次才恢复，且只恢复一次")
        self.assertFalse(a.hidden_by_guard)

    def test_two_faces_while_hidden_cannot_restore(self):
        """收起期间第二张脸一直在，恢复计数不能被「偶尔只检到一张」推进。"""
        a = PresenceArbiter()
        a.feed(2, now=0.0)
        a.feed(2, now=1.0)              # 已收起
        for i in range(a.restore_hits + 2):
            a.feed(1, now=2.0 + i)      # 差一次没满
            a.feed(2, now=2.5 + i)      # 又看到两张 → 计数归零
        self.assertTrue(a.hidden_by_guard, "第二张脸仍在时不应恢复")

    def test_idle_timeout_is_reported_once(self):
        a = PresenceArbiter(max_idle_sec=10.0)
        a.feed(2, now=0.0)
        self.assertEqual(a.feed(2, now=1.0), EVENT_HIDE)
        self.assertEqual(a.feed(0, now=5.0), EVENT_NONE)
        self.assertEqual(a.feed(0, now=12.0), EVENT_IDLE)
        self.assertEqual(a.feed(0, now=30.0), EVENT_NONE,
                         "超时只报一次：报完调用方就停摄像头了")

    def test_reset_clears_hidden_state(self):
        a = PresenceArbiter()
        a.feed(2, now=0.0)
        a.feed(2, now=1.0)
        a.reset()
        self.assertFalse(a.hidden_by_guard)
        self.assertIsNone(a.last_faces)


class TestFaceCounting(SFCase):
    """计数口径是纯函数，不需要真 Vision 也不需要摄像头。"""

    def test_low_confidence_boxes_are_not_counted(self):
        obs = [_Obs(0.9), _Obs(0.85), _Obs(FACE_GUARD_CONF_MIN - 0.3)]
        self.assertEqual(count_faces(obs), 2,
                         "低于置信度下限的框不计：远处/边缘的假框不该把窗收掉")

    def test_unreadable_confidence_counts_as_one_face(self):
        class _NoConf:
            def confidence(self):
                raise RuntimeError("这个框读不出置信度")
        self.assertEqual(count_faces([_NoConf()]), 1,
                         "读不出来时按一张脸计 —— 漏掉一个人的代价高于多收一次窗")

    def test_empty_and_none_results(self):
        self.assertEqual(count_faces([]), 0)
        self.assertEqual(count_faces(None), 0, "Vision 返回 None 不能把采集线程炸掉")


class TestMonitorDegradation(SFCase):
    """起不来时必须给出**可读、可显示**的状态，而不是抛异常或假装在跑。"""

    _CLS = {"AVCaptureDevice": object()}

    def _patched(self, **overrides):
        cls = overrides.get("cls", self._CLS)
        self.track_patch(patch.object(sf_presence, "_load_frameworks",
                                      return_value=(cls, overrides.get("why", ""))))
        if "device" in overrides:
            self.track_patch(patch.object(sf_presence, "_camera_devices",
                                          return_value=overrides["device"]))
        if "status" in overrides:
            self.track_patch(patch.object(sf_presence, "camera_access_status",
                                          return_value=overrides["status"]))

    def test_frameworks_missing_reports_unsupported(self):
        self._patched(cls=None, why="Vision 框架取不到")
        mon = FacePresenceMonitor()
        self.assertEqual(mon.start(), STATE_UNSUPPORTED)
        self.assertIn("Vision", mon.last_error)

    def test_no_camera_reports_no_camera(self):
        self._patched(device=None)
        mon = FacePresenceMonitor()
        self.assertEqual(mon.start(), STATE_NO_CAMERA)

    def test_denied_permission_is_visible_not_silent(self):
        self._patched(device=object(), status=STATE_DENIED)
        mon = FacePresenceMonitor()
        self.assertEqual(mon.start(), STATE_DENIED,
                         "权限被拒必须原样成为状态，界面才有东西可显示")

    def test_starting_never_opens_a_permission_dialog(self):
        """v1.4.47 用户要求：程序自己**永远不许**弹系统授权框。

        旧行为在产物是 ad-hoc 签名时有个真实后果：每次更新重装后 TCC 把程序当新的，
        状态回到 undetermined，于是开机第一次启动就弹一个框 —— 用户什么都没做
        就被要求当场决定。现在请求只挂在菜单点击那一步（见下面两条），
        这一条锁住"被动路径不请求"。
        """
        self._patched(device=object(), status="undetermined")
        asked = []
        self.track_patch(patch.object(FacePresenceMonitor, "_request_access",
                                      lambda self: asked.append(1)))
        self.track_patch(patch.object(sf_presence, "_ACCESS_ASKED_AT", None))
        mon = FacePresenceMonitor()
        state = mon.start()
        mon.start()
        self.assertEqual(asked, [], "start() 不许发起系统授权请求")
        self.assertEqual(state, sf_presence.STATE_NEEDS_AUTH)
        self.assertIn("菜单", mon.last_error, "状态里要说清怎么才能开始保护，不能只说没权限")
        # v1.4.50：这一行会直接显示给用户，指的路必须是屏幕上真实存在的那一项
        # （`GUARD_MENU_TEXT` 同时被用来查菜单项，所以它就是"屏幕上那个名字"）。
        # 旧文案写的是「人脸在场守卫」，用户照着找找不到 —— 台账 E02 复发的一环。
        self.assertIn(GUARD_MENU_TEXT, mon.last_error,
                      "未授权提示必须指着真实菜单项文案，got %r" % mon.last_error)

    def test_permission_is_asked_only_once_within_the_cooldown(self):
        """冷却窗口内的连点只许弹一次框（v1.4.45 的原意，v1.4.57 收窄为"窗口内"）。"""
        self._patched(device=object(), status="undetermined")
        asked = []
        self.track_patch(patch.object(FacePresenceMonitor, "_request_access",
                                      lambda self: asked.append(1)))
        self.track_patch(patch.object(sf_presence, "_ACCESS_ASKED_AT", None))
        mon = FacePresenceMonitor()
        first = mon.request_access_once()
        second = mon.request_access_once()
        self.assertEqual(asked, [1], "连着两下点击只应该请求一次摄像头授权")
        self.assertEqual((first, second), ("requested", "asked"))

    def test_ask_latch_expires_so_a_missed_prompt_is_recoverable(self):
        """v1.4.57 修的就是这条：错过系统弹窗之后，再点一次必须还能把框叫出来。

        本机 16:36 的日志实证：第一下 requested（弹窗出来了但没被作答），随后四下
        全部 "asked" —— 旧实现是进程级**永久**布尔，这个菜单项在本进程里再也不会
        有任何反应，用户唯一出路是重启程序。冷却窗口到点之后必须重新请求。
        """
        self._patched(device=object(), status="undetermined")
        asked = []
        self.track_patch(patch.object(FacePresenceMonitor, "_request_access",
                                      lambda self: asked.append(1)))
        self.track_patch(patch.object(sf_presence, "_ACCESS_ASKED_AT", None))
        mon = FacePresenceMonitor()
        self.assertEqual(mon.request_access_once(), "requested")
        self.assertEqual(mon.request_access_once(), "asked")
        self.assertEqual(asked, [1], "先把「还在冷却里」这条路验实")
        # 把"上一次问过"推到冷却窗口之外（等价于用户隔了几秒又点一次）。
        # 用 _long_ago 而不是 monotonic()-常数：开机不足一小时时后者是负数（§6 第 4 条）。
        sf_presence._ACCESS_ASKED_AT = _long_ago(   # discipline-ok(R5): 冷却账本只有这一个内部入口
            sf_constants.FACE_GUARD_REASK_COOLDOWN_SEC + 5.0)
        self.assertEqual(FacePresenceMonitor().request_access_once(), "requested",
                         "冷却到点后再点必须重新请求权限，否则菜单项就是永久空操作")
        self.assertEqual(asked, [1, 1], "重问要真的走到系统请求那一步")

    def test_permission_ask_is_process_wide_not_per_monitor(self):
        """复核 A4（v1.4.45）：授权是进程级动作 —— 新建 monitor 不该把没答的框再弹一遍。

        场景不变：用户点了开启、框出来了但没答，随后经历停→起（或换了 monitor 实例）。
        原来"问过没问过"记在实例上，换一个实例就归零；v1.4.47 把发起点收到菜单点击之后，
        这条进程级去重必须仍然成立，否则一次点击会连着弹三个框。
        （v1.4.57 把它从"永久"改成"冷却窗口内"，到点之后可重问 —— 见上一条用例。）
        """
        self._patched(device=object(), status="undetermined")
        asked = []
        self.track_patch(patch.object(FacePresenceMonitor, "_request_access",
                                      lambda self: asked.append(1)))
        self.track_patch(patch.object(sf_presence, "_ACCESS_ASKED_AT", None))
        self.assertEqual(FacePresenceMonitor().request_access_once(), "requested")
        self.assertEqual(FacePresenceMonitor().request_access_once(), "asked")
        self.assertEqual(asked, [1], "换一个 monitor 实例也不该再问一次")

    def test_already_authorized_never_asks_again(self):
        """已经授权过（状态 authorized）时点开启不该再弹框，直接走建会话那条路。"""
        self._patched(device=object(), status="authorized")
        asked = []
        self.track_patch(patch.object(FacePresenceMonitor, "_request_access",
                                      lambda self: asked.append(1)))
        self.track_patch(patch.object(sf_presence, "_ACCESS_ASKED_AT", None))
        out = FacePresenceMonitor().request_access_once()
        self.assertEqual(asked, [], "已有权限还去请求 = 无谓打扰")
        self.assertNotEqual(out, "requested")

    def test_camera_status_maps_authorization_codes(self):
        """状态码 → 可读名。写错一个映射，界面上就会把「已拒绝」显示成「未开启」。"""
        codes = {0: "undetermined", 1: STATE_RESTRICTED, 2: STATE_DENIED,
                 3: "authorized", 9: "unknown:9"}

        class _Dev:
            code = 3

            @classmethod
            def authorizationStatusForMediaType_(cls, media):
                return cls.code

        self.track_patch(patch.object(sf_presence, "_load_frameworks",
                                      return_value=({"AVCaptureDevice": _Dev}, "")))
        for code, name in codes.items():
            _Dev.code = code
            self.assertEqual(sf_presence.camera_access_status(), name,
                             "授权码 %s 应显示成 %s" % (code, name))

    def test_device_lookup_falls_back_when_default_misses(self):
        """取不到 defaultDevice 时退回全量枚举：外接摄像头就可能走这条。"""
        sentinel = object()

        class _Dev:
            @classmethod
            def defaultDeviceWithMediaType_(cls, media):
                return None

            @classmethod
            def devicesWithMediaType_(cls, media):
                return [sentinel]

        self.assertIs(sf_presence._camera_devices(          # discipline-ok(R5): 设备查找没有公开入口，这两条测的就是它的兜底分支
            {"AVCaptureDevice": _Dev}), sentinel)

    def test_device_lookup_returns_none_when_nothing_found(self):
        class _Dev:
            @classmethod
            def defaultDeviceWithMediaType_(cls, media):
                return None

            @classmethod
            def devicesWithMediaType_(cls, media):
                return []

        self.assertIsNone(sf_presence._camera_devices(      # discipline-ok(R5): 同上，取不到设备时的 None 收口只能直接调
            {"AVCaptureDevice": _Dev}))

    def test_media_type_prefers_the_bridge_constant(self):
        """视频介质常量取桥接里的官方值，取不到才退回字面量。

        这条是本轮那三个新依赖的"用到为止"证明：如果哪天 requirements 里的
        pyobjc-framework-AVFoundation 被删，本用例仍绿（走退路），但
        test_permission_is_asked_only_once 那条会开始报 UNSUPPORTED ——
        两个一起看才不会把"缺依赖"误判成"代码写错"。
        """
        expected = sf_presence._MEDIA_VIDEO_FALLBACK   # discipline-ok(R5): 测的就是这条退路本身，只能引用它
        try:
            import AVFoundation
            expected = str(AVFoundation.AVMediaTypeVideo)
        except Exception:
            pass
        self.assertEqual(sf_presence._media_video(), expected)   # discipline-ok(R5): 介质常量没有对外读取口
        self.assertEqual(sf_presence._media_video(), "vide",   # discipline-ok(R5): 本机实测值，改动要留痕
                         "本机实测的 AVCaptureMediaType 字面值，改动要写进交付记录")

    def test_request_access_needs_the_bridge_and_says_so(self):
        """缺 AVFoundation 桥接时不许"看起来在等用户"，要直接报不可用。

        这是本轮实机踩出来的那条：没有桥接元数据，block 参数调不动，
        授权框永远不会弹 —— 若此时状态还是 starting，用户就对着一个不存在的
        弹窗无限等。日志里那条 ERROR 也必须写下来，不然装机版排查时无从对齐。
        """
        mon = FacePresenceMonitor()
        mon._cls = {"AVCaptureDevice": object()}          # discipline-ok(R5): 注入假类表，见类文档
        self.track_patch(patch.dict(sys.modules, {"AVFoundation": None}))
        mon._request_access()                    # discipline-ok(R5): 授权发起点，公开路径要真弹系统框，用例里只能直接调
        self.assertEqual(mon.state, STATE_UNSUPPORTED)
        self.assertIn("AVFoundation", mon.last_error)

    def test_request_access_calls_the_device_once(self):
        """桥接在位时，走的是 requestAccessForMediaType:completionHandler: 这条路。"""
        calls = []

        class _Dev:
            @classmethod
            def requestAccessForMediaType_completionHandler_(cls, media, handler):
                calls.append(media)

        mon = FacePresenceMonitor()
        mon._cls = {"AVCaptureDevice": _Dev}              # discipline-ok(R5): 注入假类表，见类文档
        mon._request_access()                    # discipline-ok(R5): 授权发起点，公开路径要真弹系统框，用例里只能直接调
        self.assertEqual(calls, [sf_presence._media_video()],   # discipline-ok(R5): 介质常量无公开读口
                         "只应发起一次授权请求")
        self.assertEqual(mon.state, "off", "发起请求不该在本函数里改状态（等回调/轮询去读）")

    def test_request_start_does_not_block_caller(self):
        """主线程调用的入口必须立刻返回：真起会话是要花几百毫秒的。"""
        self._patched(device=object(), status=STATE_DENIED)
        mon = FacePresenceMonitor()
        t0 = time.monotonic()
        state = mon.request_start()
        self.assertLess(time.monotonic() - t0, 0.5,
                        "request_start 不该同步去做采集会话")
        self.assertEqual(state, STATE_STARTING)
        # 等后台那一段真的跑完再结束用例：不然 patch 在它还挂着的时候被撤掉，
        # 线程里的异常会打到下一个用例的输出里，看起来像是别处坏了。
        self.assertTrue(self.wait_for(lambda: not mon.is_starting(), timeout=3.0),
                        "后台拉起线程应自行收口")

    def test_start_queued_behind_the_lock_is_discarded_after_a_stop(self):
        """S1 那枚 P0：worker 排队等锁期间用户关掉守卫，这次启动必须作废。

        旧实现 `stop()` 只把 state 写成 OFF，而 `_start_locked` 只拦 `RUNNING` ——
        晚一步拿到锁的 worker 照常建会话，于是"关掉守卫之后菜单栏绿灯过一会儿自己
        亮起，而且再也停不掉"（主窗那时已把 monitor 丢弃成 None，没人再去 stop）。
        这里用**真 monitor**（只桩掉框架与 `_build_session`），因为竞态就在那把锁上，
        换成桩等于把要证的东西假设掉。
        """
        self._patched(device=object(), status="authorized")
        built = []
        self.track_patch(patch.object(FacePresenceMonitor, "_build_session",
                                      lambda self, *a, **k: built.append(1)))
        mon = FacePresenceMonitor()
        mon._api_lock.acquire()      # discipline-ok(R5): 要模拟"会话正在后台拉起、锁被占住"，这把锁没有公开入口
        self.assertEqual(mon.request_start(), STATE_STARTING)
        self.assertFalse(mon.stop(wait=False), "锁被占住时交互停不许等（复核 F5）")
        mon._api_lock.release()      # discipline-ok(R5): 同上 —— 放行后 worker 与补停线程才去抢锁
        self.assertTrue(self.wait_for(lambda: not mon.is_starting(), timeout=3.0),
                        "两条后台线程都应自行收口")
        self.assertEqual(built, [], "已经被关掉的守卫不许自己把摄像头打开")
        self.assertEqual(mon.state, "off")


class TestFrameRetention(SFCase):
    """隐私不变量：帧只路过，不留痕。

    为什么直接驱动采集回调：`_on_frame` 是采集线程唯一的入口，走公开 API 就必须真起
    AVCaptureSession（要摄像头硬件 + 系统授权弹窗），那是实机验证的活，不该塞进回归
    用例。这里要证的正是「回调里发生的事不会把帧留下来」，只能从这一个口子进。
    """

    class _Req:
        def __init__(self, confidences):
            self._confidences = confidences

        def alloc(self):
            return self

        def init(self):
            return self

        def results(self):
            return [_Obs(c) for c in self._confidences]

    class _Handler:
        def alloc(self):
            return self

        def initWithCMSampleBuffer_options_(self, buffer, options):
            self.seen_buffer = buffer
            return self

        def performRequests_error_(self, requests, error):
            return True

    def _drive_one_frame(self, mon, buffer, confidences):
        mon._cls = {                                   # discipline-ok(R5): 采集会话的唯一注入口，见类文档
            "VNDetectFaceRectanglesRequest": self._Req(confidences),
            "VNImageRequestHandler": self._Handler()}
        mon.state = STATE_RUNNING
        mon._last_detect = 0.0                         # discipline-ok(R5): 按节拍的丢弃开关，不设就等于没进检测
        mon._on_frame(None, buffer, None)              # discipline-ok(R5): 同上，只能从采集回调入口驱动
        return mon.snapshot()

    def test_frame_object_is_not_retained(self):
        buffer = object()
        mon = FacePresenceMonitor()
        count, ts = self._drive_one_frame(mon, buffer, [0.9])
        self.assertEqual(count, 1, "只允许留下一个整数计数")
        self.assertGreater(ts, 0.0, "以及这次采样的时刻")
        self.assertNotIn(buffer, list(vars(mon).values()), "采集帧不得存进 monitor 的任何字段")

    def test_detection_failure_leaves_a_trace(self):
        """检测抛异常不能变成「功能静默失效」：必须走 log_swallow 留 SWALLOWED 痕。"""
        mon = FacePresenceMonitor()

        class _Boom:
            def alloc(self):
                raise RuntimeError("Vision 拒绝处理这帧")

        swallowed = []
        self.track_patch(patch.object(sf_presence, "log_swallow",
                                      lambda *args: swallowed.append(args)))
        self._drive_one_frame(mon, object(), [0.9])
        mon._cls["VNDetectFaceRectanglesRequest"] = _Boom()   # discipline-ok(R5): 见类文档
        mon._last_detect = 0.0                               # discipline-ok(R5): 见类文档
        mon._on_frame(None, object(), None)                  # discipline-ok(R5): 见类文档
        self.assertEqual(len(swallowed), 1,
                         "兜异常必须留痕，且异常不许抛到采集线程外面")
        self.assertEqual(mon.detect_errors, 1,
                         "异常还要计入 detect_errors：状态行靠它把『认不出脸』说出来")

    # ---- v1.4.50：日志与状态行必须能一句话回答"帧到底来没来" ----
    def test_perform_requests_false_counts_as_detect_error(self):
        """`performRequests:error:` 返回 NO 时以前**返回值被丢掉**，等于无声失效。

        这一条守的就是那个丢掉的动作：Vision 侧失败但不抛异常（真机上 frame 格式
        不支持、朝向不对都是这个形状）时，计数必须涨、必须留痕，而快照不能被认为
        "数过一次"。台账 E02 复发的定位时长全靠这两个整数。
        """
        mon = FacePresenceMonitor()
        logged = []
        self.track_patch(patch.object(sf_presence, "log_line_throttled",
                                      lambda *a, **k: logged.append((a, k))))

        class _Nope(self._Handler):
            def performRequests_error_(self, requests, error):
                return False

        mon._cls = {                                         # discipline-ok(R5): 采集回调唯一注入口
            "VNDetectFaceRectanglesRequest": self._Req([0.9]),
            "VNImageRequestHandler": _Nope()}
        mon.state = STATE_RUNNING
        mon._last_detect = 0.0                               # discipline-ok(R5): 不设就等于没进检测
        mon._on_frame(None, object(), None)                  # discipline-ok(R5): 同上
        self.assertEqual(mon.detect_errors, 1)
        self.assertEqual(mon.frames, 0, "没数成一次就不能算来_over 一帧")
        self.assertEqual(mon.snapshot(), (0, 0.0), "失败帧不许写进快照")
        self.assertTrue(logged, "检测失败必须留日志，否则事后无从查起")

    def test_first_frame_logs_one_anchor(self):
        """首帧锚点只写一行：日志里搜 `face_guard 首帧` 就证明整条链通过了。"""
        mon = FacePresenceMonitor()
        lines = []
        self.track_patch(patch.object(sf_presence, "log_line",
                                      lambda *a, **k: lines.append(a)))
        self._drive_one_frame(mon, object(), [0.9, 0.8])   # 两张脸的那一帧
        self._drive_one_frame(mon, object(), [0.9])
        self.assertEqual(mon.frames, 2)
        anchors = [x for x in lines if "首帧" in str(x)]
        self.assertEqual(len(anchors), 1, "首帧锚点应当恰好一行，got %r" % lines)
        self.assertIn("2 张脸", str(anchors[0]),
                      "锚点要带上数到的脸数，事后才知道链通了、判据对不对")

    def test_stop_clears_counters_and_snapshot(self):
        """停表后计数归零：诊断包不能把上一段会话的帧数当成"现在还在保护"的证据。"""
        mon = FacePresenceMonitor()
        self._drive_one_frame(mon, object(), [0.9])
        self.assertEqual(mon.frames, 1)
        self.assertTrue(mon.stop(wait=True))
        self.assertEqual((mon.frames, mon.detect_errors), (0, 0))
        self.assertEqual(mon.snapshot(), (0, 0.0))


class TestExpeditedRecheck(SFCase):
    """v1.4.56 事件驱动加急复检：计数一变化就提前再数一次，稳态不涨功耗。"""

    class _Req:
        def __init__(self, confidences):
            self._c = confidences

        def alloc(self):
            return self

        def init(self):
            return self

        def results(self):
            return [_Obs(x) for x in self._c]

    class _Handler:
        def alloc(self):
            return self

        def initWithCMSampleBuffer_options_(self, buffer, options):
            return self

        def performRequests_error_(self, requests, error):
            return True

    def _feed(self, mon, confidences):
        mon._cls = {                                       # discipline-ok(R5): 采集回调唯一注入口
            "VNDetectFaceRectanglesRequest": self._Req(confidences),
            "VNImageRequestHandler": self._Handler()}
        mon.state = STATE_RUNNING
        mon._last_detect = 0.0                             # discipline-ok(R5): 不设就等于没进检测
        mon._on_frame(None, object(), None)                # discipline-ok(R5): 同上

    # 加急之后允许的调度抖动余量：帧到达时刻与 monotonic() 取值之间总有毫秒级差，
    # 不留这一点就会在机器忙时假红；它也远小于一个节拍（0.5s），不会把"没加急"放过去。
    JITTER_SEC = 0.05

    def test_count_change_arms_expedited_recheck(self):
        mon = FacePresenceMonitor()
        self._feed(mon, [0.9])                             # 1 张脸（稳态）
        self._feed(mon, [0.9, 0.9])                        # 第二张脸进来 = 变化
        # 「下一次何时允许检测」只记在 `_last_detect` 上，没有对外读取口 ——
        # 不加急就等于这条判据从没生效过，只能从这里取证。
        wait = mon.tick_sec - (time.monotonic() - mon._last_detect)   # discipline-ok(R5): 见上一行
        self.assertLessEqual(wait, sf_constants.FACE_GUARD_EXPEDITE_SEC + self.JITTER_SEC,
                             "变化之后下一次检测应当几乎不用等（实测还要等 %.0fms）"
                             % (max(0.0, wait) * 1000))
        self.assertTrue(mon.expedited, "计数变化必须记下一次加急（否则这条判据从没生效过）")

    def test_steady_count_never_expedites(self):
        """稳态必须还是半秒一拍 —— 否则"稳态功耗没涨"这句话就是假的。"""
        mon = FacePresenceMonitor()
        for _ in range(6):
            self._feed(mon, [0.9])
        self.assertFalse(mon.expedited, "没有变化就不该加急，稳态占空比不许涨")
        self.assertGreaterEqual(
            mon.tick_sec - (time.monotonic() - mon._last_detect),   # discipline-ok(R5): 节拍状态只有这一个内部字段
            mon.tick_sec * 0.6,                            # 剩一大半节拍才算"按整拍在等"
            "没变化时不许把节拍提前")

    def test_expedite_budget_caps_the_rate(self):
        """两个人来回走动（计数反复变化）时，每秒最多加急预算那么多次。"""
        mon = FacePresenceMonitor()
        for i in range(12):
            self._feed(mon, [0.9] if i % 2 == 0 else [0.9, 0.9])
        self.assertLessEqual(mon.expedited, sf_constants.FACE_GUARD_EXPEDITE_BUDGET,
                             "加急没有上限就会把占空比顶满（省电那条就白做了）")


class _RecordingSession:
    """假 AVCaptureSession：只记录被调用的方法，用来断言搭建/收尾的顺序。"""

    def __init__(self):
        self.calls = []
        self.running = False

    def canSetSessionPreset_(self, preset):
        self.calls.append(("canSetSessionPreset", preset))
        return True

    def setSessionPreset_(self, preset):
        self.calls.append(("setSessionPreset", preset))

    def canAddInput_(self, inp):
        self.calls.append(("canAddInput", inp))
        return True

    def addInput_(self, inp):
        self.calls.append(("addInput", inp))

    def canAddOutput_(self, out):
        self.calls.append(("canAddOutput", out))
        return True

    def addOutput_(self, out):
        self.calls.append(("addOutput", out))

    def startRunning(self):
        self.calls.append(("startRunning",))
        self.running = True

    def stopRunning(self):
        self.calls.append(("stopRunning",))
        self.running = False

    def alloc(self):
        return self

    def init(self):
        return self


class _FakeOutput:
    def __init__(self):
        self.discards_late = None
        self.delegate = None
        self.queue = None
        # 记录委托的挂上/摘下顺序：停会话时"先摘委托再 stopRunning"是防悬垂引用的硬要求
        self.delegate_calls = []

    def setAlwaysDiscardsLateVideoFrames_(self, v):
        self.discards_late = bool(v)

    def setSampleBufferDelegate_queue_(self, delegate, queue):
        self.delegate_calls.append(("attach" if delegate is not None else "detach",
                                    delegate, queue))
        self.delegate = delegate
        self.queue = queue

    def alloc(self):
        return self

    def init(self):
        return self


class _FakeDevice:
    """假 AVCaptureDevice：记录功耗画像相关的配置调用（v1.4.54）。

    `lock` 返回值默认按真机形状造 —— PyObjC 对带 NSError 出参的方法返回
    (BOOL, error) 元组（v1.4.49 建会话栽过的同一种形状），这里故意不给单 bool。
    """

    def __init__(self, lock_result=(True, None), fail_sets=False):
        self.lock_result = lock_result
        self.fail_sets = fail_sets
        self.calls = []

    def lockForConfiguration_(self, err):
        self.calls.append(("lock",))
        return self.lock_result

    def unlockForConfiguration(self):
        self.calls.append(("unlock",))

    def setActiveVideoMinFrameDuration_(self, dur):
        self.calls.append(("min_fps", dur))
        if self.fail_sets:
            raise ValueError("NSInvalidArgumentException - 不支持这个帧率")

    def setActiveVideoMaxFrameDuration_(self, dur):
        self.calls.append(("max_fps", dur))
        if self.fail_sets:
            raise ValueError("NSInvalidArgumentException - 不支持这个帧率")


class _FakeNSObject:
    """`_make_sink` 用它做基类；只需要能被实例化。"""

    @classmethod
    def new(cls):
        return cls()


class TestSessionPlumbing(SFCase):
    """会话搭建与收尾：用假系统类把这条路径完整走一遍。

    为什么值得测：这条路径是真机上唯一需要摄像头硬件才能验证的，一旦写错（忘记
    stopRunning、或者 delegate 被 GC）在回归里完全看不出来，只会表现成"实机灯一直亮"
    或"跑一会儿就不出计数了"。这里用假类把调用序列钉住，实机只需再确认一次真帧。
    """

    def _cls_table(self, session, output):
        class _DeviceInput:
            @staticmethod
            def deviceInputWithDevice_error_(device, err):
                return ("input", device)

        class _Handler:
            def alloc(self):
                return self

            def initWithCMSampleBuffer_options_(self, buffer, options):
                return self

            def performRequests_error_(self, requests, error):
                return True

        class _Req:
            def alloc(self):
                return self

            def init(self):
                return self

            def results(self):
                return [_Obs(0.9), _Obs(0.9)]

        return {
            "AVCaptureSession": session,
            "AVCaptureDeviceInput": _DeviceInput,
            "AVCaptureVideoDataOutput": output,
            "VNImageRequestHandler": _Handler(),
            "VNDetectFaceRectanglesRequest": _Req(),
            "NSObject": _FakeNSObject,
        }

    def _start_with_fakes(self, device=None):
        session = _RecordingSession()
        output = _FakeOutput()
        table = self._cls_table(session, output)
        mon = FacePresenceMonitor()
        mon._cls = table                                   # discipline-ok(R5): 注入假系统类，见类文档
        self.track_patch(patch.object(sf_presence, "_camera_devices",
                                      return_value=device or object()))
        self.track_patch(patch.object(sf_presence, "camera_access_status",
                                      return_value="authorized"))
        self.track_patch(patch.object(sf_presence, "_load_frameworks",
                                      return_value=(table, "")))
        state = mon.start()
        return mon, session, output, state

    def test_start_builds_session_in_order(self):
        mon, session, output, state = self._start_with_fakes()
        self.assertEqual(state, STATE_RUNNING)
        names = [c[0] for c in session.calls]
        self.assertEqual(names, ["canSetSessionPreset", "setSessionPreset",
                                 "canAddInput", "addInput",
                                 "canAddOutput", "addOutput", "startRunning"],
                         "搭建顺序：功耗档位 → 先验后加，全部就绪才 startRunning")
        self.assertIsNotNone(output.delegate, "委托对象必须被输出通道接住")
        self.assertIsNotNone(output.queue, "必须给采集一条自己的队列，不能投到主线程")
        self.assertTrue(output.discards_late,
                        "迟帧要丢弃：要的是最近有没有两张脸，不是每一帧")

    def test_stop_releases_session_and_clears_counts(self):
        mon, session, output, _ = self._start_with_fakes()
        mon.stop()
        self.assertIn(("stopRunning",), session.calls,
                      "停表必须真的 stopRunning，否则绿灯不会灭")
        self.assertEqual(mon.snapshot(), (0, 0.0), "停用后不留旧计数，防止下一轮误判")
        self.assertEqual(mon.state, "off")

    def test_delegate_is_detached_before_session_stops(self):
        """复核 F4：摘委托 → stopRunning → 最后才丢引用，这个顺序不能反。

        委托对象由 Python 侧持有，而 AVFoundation 那边是**不持有**的裸引用；
        先 stopRunning 再丢引用时，投递队列上已排队的帧回调可能在对象回收后抵达，
        那是只在"刚关功能正好有帧在飞"时才爆的随机崩溃，事后查不到。
        """
        mon, session, output, _ = self._start_with_fakes()
        mon.stop()
        self.assertEqual([c[0] for c in output.delegate_calls], ["attach", "detach"],
                         "停会话必须先把委托从输出通道上摘下来")
        self.assertEqual(session.calls[-1][0], "stopRunning",
                         "摘完委托才 stopRunning，顺序反了就等于边收线边让帧回调进来")
        self.assertIsNone(output.delegate_calls[-1][1], "摘下来时传的必须是 None")
        self.assertIsNone(mon._delegate,                          # discipline-ok(R5): 验的正是引用释放时机
                          "引用要留到 stopRunning 之后再放")

    # ---- v1.4.54 功耗画像：分辨率档位 + 帧率上限（先实测再动，fail-open）----
    def test_session_sets_power_preset_before_inputs(self):
        mon, session, _, _ = self._start_with_fakes()
        names = [c[0] for c in session.calls]
        self.assertLess(names.index("setSessionPreset"), names.index("addInput"),
                        "功耗档位必须在挂输入之前定好（会话跑起来再改就晚了）")
        self.assertIn(("setSessionPreset", sf_constants.FACE_GUARD_CAMERA_PRESET),
                      session.calls, "档位必须来自常量，不许写字面量漂移")

    def test_device_frame_rate_capped_then_unlocked(self):
        from CoreMedia import CMTimeMake
        dev = _FakeDevice()
        mon, _, _, state = self._start_with_fakes(device=dev)
        self.assertEqual(state, STATE_RUNNING)
        kinds = [c[0] for c in dev.calls]
        self.assertEqual(kinds, ["lock", "min_fps", "max_fps", "unlock"],
                         "加锁 → 设上下限 → 解锁，一次都不能少")
        for kind in ("min_fps", "max_fps"):
            dur = next(c[1] for c in dev.calls if c[0] == kind)
            self.assertEqual((int(dur.value), int(dur.timescale)),
                             (1, sf_constants.FACE_GUARD_CAMERA_FPS),
                             "%s 必须是 1/%ds" % (kind, sf_constants.FACE_GUARD_CAMERA_FPS))

    def test_lock_denial_does_not_touch_frame_rate(self):
        """lock 返回 (False, err) 时绝不能继续设帧率 —— 元组形状不解包就会漏过去。"""
        dev = _FakeDevice(lock_result=(False, "拒绝"))
        mon, _, _, state = self._start_with_fakes(device=dev)
        self.assertEqual(state, STATE_RUNNING, "锁拿不到也要照常起会话（fail-open）")
        self.assertEqual([c[0] for c in dev.calls], ["lock"],
                         "锁被拒后不许碰帧率，也不许假装解锁成功")

    def test_device_rejecting_frame_rate_still_runs(self):
        """设备拒绝 1/15s（本机实测 1/10s 就是这样被拒的）只能记日志，不能弄挂会话。"""
        dev = _FakeDevice(fail_sets=True)
        mon, _, _, state = self._start_with_fakes(device=dev)
        self.assertEqual(state, STATE_RUNNING,
                         "省电失败可以接受，保护失败不可以")
        kinds = [c[0] for c in dev.calls]
        self.assertIn("unlock", kinds, "设帧率炸了也必须解锁，否则设备配置锁被永久占住")

    def test_session_stops_even_when_delegate_detach_fails(self):
        """摘委托失败绝不能跳过 stopRunning —— 那会变成"摄像头停不下来、绿灯常灭不掉"。

        这条是修 F4 时我自己写出来的新坑：第一版为了"不制造悬垂引用"在摘失败时直接
        return，结果连停会话都跳过了。正确做法是两件事都要：会话照停，摘不下来的
        委托进模块级暂存区，绝不让一个可能还在被投递帧的对象先被 GC 掉。
        """
        mon, session, output, _ = self._start_with_fakes()

        def _boom(delegate, queue):
            raise RuntimeError("setter 炸了")

        output.setSampleBufferDelegate_queue_ = _boom
        parked_before = len(sf_presence._PARKED_SINKS)        # discipline-ok(R5): 验的正是兜底暂存是否生效
        mon.stop()
        parked_after = len(sf_presence._PARKED_SINKS)         # discipline-ok(R5): 同上，停完之后再看一次
        self.assertIn(("stopRunning",), session.calls, "摘委托失败也必须把会话停下来")
        self.assertEqual(parked_after, parked_before + 1,
                         "摘不下来的委托要被暂存住，不能交给 GC")
        self.assertEqual(mon.state, "off")

    def test_delegate_reports_face_count(self):
        """AVFoundation 投递给委托的那一拍，最终变成一个整数计数。"""
        mon, session, output, _ = self._start_with_fakes()
        delegate = output.delegate
        delegate.captureOutput_didOutputSampleBuffer_fromConnection_(
            output, object(), None)
        count, ts = mon.snapshot()
        self.assertEqual(count, 2, "两帧都算数的脸 → 计数 2，主窗据此收窗")
        self.assertGreater(ts, 0.0)

    def test_tick_throttles_detection(self):
        """一秒节拍：连续两拍只检一次，省下的就是 CPU 和电。"""
        mon, session, output, _ = self._start_with_fakes()
        delegate = output.delegate
        mon._last_detect = 0.0                               # discipline-ok(R5): 节拍开关
        delegate.captureOutput_didOutputSampleBuffer_fromConnection_(output, object(), None)
        first = mon.snapshot()
        delegate.captureOutput_didOutputSampleBuffer_fromConnection_(output, object(), None)
        self.assertEqual(mon.snapshot(), first, "同一节拍内的第二帧应被直接丢弃")

    def test_dispatch_queue_is_reusable_object(self):
        """ctypes 取的 GCD 队列要能被 PyObjC 包成 AVFoundation 接受的参数。"""
        q = sf_presence._dispatch_queue()   # discipline-ok(R5): 队列是 ctypes 取指针再包成 ObjC 对象，只有真调一次才知道包法成立
        self.assertIsNotNone(q)
        self.assertIn("dispatch_queue", str(type(q)).lower(),
                      "包出来的应该是 OS_dispatch_queue，不是裸整数指针")
        self.assertIs(sf_presence._dispatch_queue(), q,   # discipline-ok(R5): 复用的语义只能连调两次来验
                      "队列必须全进程复用一条：每次新建就会只增不减地漏")

    def test_frameworks_load_on_this_mac(self):
        """真装载一次：这台机器上 7 个系统类都必须取到。

        不需要摄像头权限 —— 取类与授权是两件事，所以这条可以留在回归里。
        """
        cls, why = sf_presence._load_frameworks()          # discipline-ok(R5): 动态装载系统框架是本轮「零新依赖」的关键，取类不需要摄像头权限，可以留在回归里
        self.assertEqual(why, "", "框架装载失败：%s" % why)
        self.assertIsNotNone(cls)
        for key in ("AVCaptureSession", "AVCaptureDevice", "AVCaptureDeviceInput",
                    "AVCaptureVideoDataOutput", "VNImageRequestHandler",
                    "VNDetectFaceRectanglesRequest", "NSObject"):
            self.assertIsNotNone(cls[key], "%s 没取到" % key)


class TestWindowWiring(SFCase):
    """主窗接线：菜单、触发、以及「谁收的窗决定摄像头停不停」。"""

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    class _StubMonitor:
        """替掉真采集：只记录调用，便于断言什么时候该停、什么时候不该停。"""

        def __init__(self, *a, **k):
            self.state = STATE_RUNNING
            self.last_error = ""
            self.conf_min = sf_presence.FACE_GUARD_CONF_MIN
            self.tick_sec = sf_presence.FACE_GUARD_TICK_SEC
            self.started = 0
            self.stopped = 0
            self.stop_waits = []          # 记录每次停表是不是允许等锁（交互路径必须 False）
            # True = 模拟"会话锁被后台启动占住"，此时 stop(wait=False) 返回 False（还没停）
            self.stop_defers = False
            self.start_state = STATE_RUNNING   # 本桩"起会话"的结果，可被用例改成 denied 等
            self.running_since = 0.0      # 零帧判定要用它当基线（真实 monitor 同名属性）
            self.faces = 0
            self.ts = None        # None = 每次现取单调钟；给定值 = 固定同一份快照
            self.asks = 0                 # 授权请求被发起的次数（v1.4.47 起的必镜像成员）
            self.ask_result = "requested"   # request_access_once 的返回值，可被用例改成 denied
            self.frames = 0               # v1.4.50：这一段会话成功数过脸的帧数（状态行要读）
            self.detect_errors = 0        # v1.4.50：帧来了但检测失败的次数

        def request_access_once(self):
            """镜像真 monitor 的唯一授权发起点：只有用户点菜单开启时才会被调到。"""
            self.asks += 1
            return self.ask_result

        def request_start(self):
            self.started += 1
            # 真实 monitor 起成功后 state 就是 running；桩也要跟上，否则重试路径
            # 会一直停在"非 running"分支，测不到"拿到新帧后撤中断标记"这半段。
            # 结果取自 start_state，让"权限被拒"那类用例还能构造出非 running 的现场。
            self.state = self.start_state
            return self.start_state

        def is_starting(self):
            return False

        def snapshot(self):
            return (self.faces, time.monotonic() if self.ts is None else self.ts)

        def stop(self, wait=True):
            self.stopped += 1
            self.stop_waits.append(bool(wait))
            if self.stop_defers and not wait:
                # 会话锁被后台启动占住：交互路径不等，返回 False = "还没停"
                return False
            self.stop_defers = False
            self.state = "off"
            return True

    def _make_monitor(self, *a, **k):
        """桩 monitor：记下真构造参数，让用例能验"回调有没有挂上"。"""
        self.stub._on_sample = k.get("on_sample")   # discipline-ok(R5): 被测对象就是这条回调接线
        return self.stub

    def setUp(self):
        super().setUp()          # SFCase 在这里建 _sf_widgets / _sf_patches 两张登记表
        self.tmp = tempfile.mkdtemp(prefix="sf_face_guard_cfg_")
        self.cfg = os.path.join(self.tmp, "config.json")
        self.bak = os.path.join(self.tmp, "config.backup.json")
        self.stub = self._StubMonitor()
        self.notices = []          # 记录"代价说明"弹了几次（真弹窗已被 setUp 拦掉）
        self._patches = [
            patch.object(m, "load_config",
                         return_value=("sh600519", 100, 100, 200, "glass")),
            # save_config 必须返回真值：否则退出收尾会以为"配置写盘失败"，
            # 在每个用例结尾刷一条 WARNING，看着像是本轮改动弄坏了落盘。
            patch.object(m, "save_config", lambda *a, **k: True),
            patch.object(m, "load_face_guard_pref", return_value=False),
            patch.object(m, "start_icon_worker", lambda: None),
            patch.object(sf_quote, "http_get", _fake_http_get),
            patch.object(m.FloatWindow, "_force_top", lambda self: None),
            patch.object(m.FloatWindow, "_setup_reopen_handler", lambda self: None),
            patch.object(sf_config, "CONFIG_PATH", self.cfg),
            patch.object(sf_config, "CONFIG_BACKUP_PATH", self.bak),
            patch("PyQt6.QtWidgets.QMenu.popup", lambda *a, **k: None),
            # 用例里绝不允许真去拉起 AVCaptureSession：一律换成桩。
            # 顺手把构造参数记下来：`on_sample` 是"数完新快照就判"的唯一接线，
            # 桩不镜像它，事件驱动那条用例就是在测空气。
            patch.object(sf_presence, "FacePresenceMonitor", self._make_monitor),
            # 缩小/恢复会动 Dock 图标与 App Nap，这些与本轮要证的事无关
            patch.object(m, "show_dock", lambda: None),
            patch.object(m.FloatWindow, "_close_target_panel"),
            patch.object(m.FloatWindow, "_update_nap_policy"),
            patch.object(m.FloatWindow, "_reassert_top"),
            patch.object(m.FloatWindow, "_sync_dot_timer"),
            # 这条 patch 是**必须**的：`sf_system_alert` 的 NSAlert 路径判据是
            # "AppKit 可用 + 主线程"，测试正好两条都满足，不拦就是在用户屏幕上摞真对话框
            # （非阻塞，测试仍秒过，只有用户看得见）。v1.4.52 起人脸守卫的开启路径
            # **不该再弹任何框**（用户要求"永不出现"），所以这里同时充当计数器：
            # `test_toggle_persists_pref_and_never_pops_a_dialog` 断言它为空。
            # 留着它而不是删掉，是因为配置写盘失败那一条路径仍然会弹框，必须拦。
            patch.object(m.FloatWindow, "_show_update_message",
                         lambda _win, *a, **k: self.notices.append(a)),
        ]
        for p in self._patches:
            self.track_patch(p)
        self.win = m.FloatWindow()
        self.track_widget(self.win)
        self.win.running = False
        self.win._wake.set()                             # discipline-ok(R5): 沿用存量夹具，唤醒后台循环线程好让它立刻退出
        self.win.timer.stop()
        self.win.show()
        QApplication.processEvents()

    def tearDown(self):
        # 顺序刻意反过来：**先**在 patch 还挂着的时候关窗。closeEvent 会走退出收尾
        # 里的 save_config，等 patch 停了再关就会用真实实现覆盖 ~/Library 下的用户配置
        # —— 本轮夹具全部离线，绝不允许写用户的盘。SFCase 之后会再关一次，是无害的。
        # 同时把窗口标记成"收尾已做过"：offscreen 下 close() 未必同步派发 closeEvent，
        # 只靠 patch 挡不住晚到的那一次落盘（run_tests.sh 的用户数据红线会抓到）。
        try:
            self.win._exit_persisted = True                # discipline-ok(R5): 退出落盘的幂等标记，测试不验证它，只必须屏蔽它
            self.win.close()
        except Exception:
            pass
        QApplication.processEvents()
        super().tearDown()
        for path in (self.cfg, self.bak):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass
        try:
            os.rmdir(self.tmp)
        except OSError:
            pass

    # ---- 辅助：全部走公开入口，内部状态只从 diagnostics 读 ----
    def _menu_action_texts(self):
        ev = QContextMenuEvent(QContextMenuEvent.Reason.Mouse,
                               QPoint(10, 10), QPoint(10, 10))
        self.win.contextMenuEvent(ev)
        texts = [a.text() for a in self._menu_actions()]
        return texts

    def _menu_actions(self):
        menu = self.win._ctx_menu                        # discipline-ok(R5): popup() 被桩掉，菜单对象只挂在这个私有引用上，存量用例同口径
        return list(menu.actions())

    def _trigger_guard_item(self):
        """点菜单里那一项 = 用户唯一的开启路径。返回触发后的 QAction。"""
        ev = QContextMenuEvent(QContextMenuEvent.Reason.Mouse,
                               QPoint(10, 10), QPoint(10, 10))
        self.win.contextMenuEvent(ev)
        hit = [a for a in self._menu_actions() if a.text() == GUARD_MENU_TEXT]
        self.assertTrue(hit, "右键菜单里没有人脸守卫项")
        hit[0].trigger()
        return hit[0]

    def _enable(self):
        self._trigger_guard_item()
        d = self.win.face_guard_diagnostics()
        self.assertTrue(d["enabled_by_user"], "菜单点完就该是开启态")
        # 这一类用例验的是"命中之后接的是哪根线"（收窗、停不停摄像头、恢复、自愈），
        # 驻留时长会把两次即时喂入挡下来 —— 那是另一件事，专门有两条用例量它
        # （TestDwellAndEventDriven）。所以这里把驻留关掉，不去改判据本身。
        a = self.win._face_guard_arbiter            # discipline-ok(R5): 关驻留只能从这张表上关
        if a is not None:
            a.dwell_sec = 0.0
        return d

    def _poll(self, times=1):
        """推进守卫节拍。"""
        for _ in range(times):
            self.win._on_face_guard_tick()               # discipline-ok(R5): 这是 QTimer 回调，等真 0.5s 节拍会给用例引入抖动

    def _state(self):
        return self.win.face_guard_diagnostics()

    def _status_line(self):
        """守卫"到底在不在跑"的那一句现文。

        v1.4.51 起它**不再显示在右键菜单里**（用户 2026-10-09 看图要求"提示语去掉，
        不展示"），但话本身必须继续存在，并且随诊断包 `face_guard.json` 的
        `status_text` 外发 —— 所以断言走这个公开读取口，而不是菜单文本。
        """
        return self.win.face_guard_status_text(self._state())

    # ---- 断言 ----
    def test_menu_shows_toggle_without_status_rows(self):
        """菜单里只有那一项开关，**不再有任何灰字提示行**（v1.4.51 用户要求）。

        守卫在不在跑仍然说真话 —— 但靠的是勾选本身（`_face_guard_protecting`），
        完整那句话改由诊断包 `status_text` 承载，见 `_status_line` 的说明。
        """
        texts = self._menu_action_texts()
        self.assertIn(GUARD_MENU_TEXT, texts)
        for noise in ("未开启", "运行中", "未授权", "保护已中断", "已暂停",
                      "自动检查", "认不出脸"):
            self.assertFalse([t for t in texts if noise in t],
                             "菜单不该再展示提示语：%r 仍出现在 %r" % (noise, texts))
        self.assertIn("未开启", self._status_line(),
                      "不展示不等于不说话：同一句话必须仍在诊断包里")

    def test_running_state_reports_the_rule_in_diagnostics(self):
        self._enable()
        line = self._status_line()
        self.assertIn("运行中", line, "已开启时状态要报告运行中，got %r" % line)
        self.assertIn("连续", line, "阈值与生效规则必须写清楚，不能只说「开着了」")

    def test_watchdog_is_not_slower_than_sampling(self):
        """看门狗兜底不得比采样节拍慢 —— 判定主通路已经是信号，这条只防"信号丢了"。

        2026-10-12 二次复核之后改了口径：判定不再靠轮询对齐（同一份快照会被去重），
        所以这里守的是"兜底不能比数据源还迟钝"。写成**相对关系**而不是等于某个毫秒数：
        等值断言是拿代码自证代码，把那一行改坏也不会红。
        """
        self.assertLessEqual(self.win._guard_timer.interval(),                   # discipline-ok(R5): 守的就是装上去的这张表
                             int(sf_presence.FACE_GUARD_TICK_SEC * 1000),
                             "看门狗比采样还慢，信号一旦没送达就要空等一个节拍以上")

    def test_same_snapshot_is_never_fed_twice(self):
        """同一份快照只算一次命中 —— 否则"连续两次命中"防的还是单帧误检吗？

        2026-10-12 二次复核 F1：以前主线程问得比采样快，一份"两张脸"的快照被连读
        两次就把窗收了，迟滞形同虚设，驻留计时也会被撑成假数字。
        这里把替身的样本时刻钉死（ts 固定 = 摄像头只数了一次），连喂三拍都不许收窗。
        """
        self._enable()
        self.stub.faces = 2
        self.stub.ts = time.monotonic()
        self._poll(3)
        self.assertTrue(self.win.isVisible(),
                        "同一份快照被喂了三次并判定成两次命中 —— 去重失效")
        self.assertFalse(self._state()["hidden_by_guard"])
        # 换一份新快照，就该立刻算第二次命中
        self.stub.ts = time.monotonic() + 1.0
        self._poll(1)
        self.assertFalse(self.win.isVisible(), "换新样本后仍不收窗：去重把正常路径也挡了")

    def test_new_sample_is_judged_without_waiting_for_the_timer(self):
        """数完新快照的判定走信号，不等那张表 —— 这是"事件驱动"那一格的全部意义。

        做法：把看门狗停掉（证明不是它干的），固定一份两张脸的快照，
        从采集线程同款位置调回调，再让主线程事件循环跑一圈。
        """
        self._enable()
        self.stub.faces = 2
        self.stub.ts = time.monotonic()
        self.win._guard_timer.stop()                # discipline-ok(R5): 要证明判定不靠这张表，必须先停它
        mon = self.win._face_guard_monitor          # discipline-ok(R5): 取本轮新建的 monitor 才能验回调
        self.assertIsNotNone(getattr(mon, "_on_sample", None),
                             "建 monitor 时没挂 on_sample 回调 = 事件驱动根本没接")
        self._poll(1)                                  # 第一次命中（建立 fed_ts）
        self.stub.ts = time.monotonic() + 2.0          # 采集线程数出了新快照
        mon._on_sample()                            # discipline-ok(R5): 采集线程回调没有公开入口
        self.assertTrue(self.win.isVisible(),
                        "排队信号不该同步就把窗收掉（那等于在采集线程动界面）")
        self._app.processEvents()
        self.assertFalse(self.win.isVisible(),
                         "信号送达后主线程没有立刻判：事件驱动这条通路没接上")

    def test_geometry_failure_leaves_a_trace(self):
        """读不到检测框几何时必须留计数 —— 降级方向保守，但不能静默。

        没有这一条的话，"边缘的人不收窗"这类事只能靠复现，日志里一个字都查不到
        （AGENTS §6 第 9 条，热路径兜异常不许静默）。
        """
        before = sf_presence.geometry_failures()
        count_faces([_ObsBox(0.80, geo=False), _ObsBox(0.80, geo=False)])
        self.assertGreaterEqual(sf_presence.geometry_failures() - before, 2,
                                "几何读不出来却没有任何计数")
        d = self._state()
        self.assertIn("geometry_failures", d, "诊断包读不到这个计数 = 事后仍然查不到")

    def test_running_status_sentence_names_every_bar(self):
        """那句人话必须把新加的三条门槛一起说出来：只写"连续 N 次"就是按旧口径说话。"""
        self._enable()
        self.win._face_guard_arbiter.dwell_sec = sf_presence.FACE_GUARD_DWELL_SEC  # discipline-ok(R5): 恢复产品值给状态文案用
        line = self._status_line()
        for frag in ("0.38", "贴边", "距离"):
            self.assertIn(frag, line, "生效规则少了一条，外面只能靠猜：%r" % frag)

    def test_diagnostics_carry_every_bar_that_judges_a_face(self):
        """诊断包要能回答"这轮按什么口径算几张脸"，否则界面上不展示之后没人说得清。

        v1.4.51/52 撤掉菜单灰字与状态行之后，`face_guard.json` 是生效规则唯一的外发通路；
        2026-10-12 加了贴边放宽与尺寸闸门两条，只写 conf_min 就等于让外面按旧口径读。
        """
        d = self._state()
        for key in ("conf_min", "edge_conf_min", "near_face_w", "max_face_width"):
            self.assertIn(key, d, "诊断包缺 %r：这条口径在外面就读不到" % key)
        self.assertLess(d["edge_conf_min"], d["conf_min"],
                        "贴边那条是放宽，不是收紧：它必须低于主阈值才有意义")
        self.assertGreater(d["near_face_w"], 0.0,
                           "尺寸闸门写成 0 等于没有这条闸门")

    def test_update_periodic_timer_uses_the_constant(self):
        """检查更新的节奏：装在真定时器上的间隔必须就是那个常量。

        为什么放这儿：v1.4.51 把菜单里那行"自动检查：启动后 15 秒…"撤掉之后，
        原来守着它的测试（test_v1444）变成"自己拼字符串自证"，产品一行都没碰。
        这条改量真实对象 —— 间隔写错就等于用户看到的更新节奏与代码不符。
        """
        from sf_constants import UPDATE_PERIODIC_MS
        timer = self.win._update_periodic_timer        # discipline-ok(R5): 间隔只在构造时写进 QTimer，没有对外读取口
        self.assertEqual(timer.interval(), UPDATE_PERIODIC_MS,
                         "周期定时器装的间隔与 sf_constants 不一致：界面撤了说明行，"
                         "更不能让常量与运行时脱钩")

    def test_menu_version_row_has_no_prefix_word(self):
        """版本那一行只写 `v<版本号>`（带阶段时加 Beta），不带"版本"两个字（v1.4.53）。

        刻意**不**断言版本号本身（AGENTS §6 第 1 条：版本唯一来源是
        `sf_constants.APP_VERSION`，源码与产物的同源性由发版链的
        `tools/dist_version_check.py` 守）。这里只守形状。
        """
        texts = self._menu_action_texts()
        rows = [t for t in texts if t.startswith("v")]
        self.assertTrue(rows, "菜单里应当有以 v 开头的版本行，got %r" % texts)
        self.assertFalse([t for t in texts if t.startswith("版本")],
                         "不该再有「版本」这两个字：" + repr(texts))

    def test_menu_check_refreshes_with_reality_while_open(self):
        """菜单开着时勾号要跟真实状态走，不能只在弹出那一瞬算一遍（v1.4.54）。

        用户实测"打勾出现和消失不够及时"：点开启后保护要经后台线程起会话，这段
        窗口期里勾是灭的；菜单开着时状态翻转也看不见。300ms 刷新一次真值是修法。
        """
        self.stub.start_state = sf_presence.STATE_NEEDS_AUTH
        self._enable()
        act = self._menu_guard_item()
        self.assertFalse(act.isChecked(), "没在保护就不该有勾（初始状态）")
        self.stub.state = STATE_RUNNING            # 后台把会话起起来了
        self.win._refresh_face_guard_menu_check()  # discipline-ok(R5): 这是定时器回调，等真 300ms 会给用例引入抖动
        self.assertTrue(act.isChecked(), "保护真起来了，勾必须跟上")
        self.stub.state = sf_presence.STATE_NEEDS_AUTH
        self.win._refresh_face_guard_menu_check()  # discipline-ok(R5): 同上
        self.assertFalse(act.isChecked(), "保护停了，勾必须及时灭掉，不能挂着骗人")
        self.win._on_ctx_menu_closed()             # discipline-ok(R5): 关菜单的公开入口是 aboutToHide，offscreen 下不保证派发
        self.assertIsNone(getattr(self.win, "_face_guard_menu_action", None),   # discipline-ok(R5): 刷新定时器没有对外读取口，验的正是这个内部收口
                          "关菜单必须连刷新定时器与动作引用一起收掉")

    # ---- v1.4.67：点开启之后，勾号在启动窗口内不许被自己的刷新定时器撤掉 ----
    # 用户报障原话「每次第一次点击都没反应，都要第二次点击才生效」。成因不是点击丢了
    # （日志里每一下都有"授权发起点=菜单点击"），而是唯一看得见的反馈（勾号）的口径是
    # state==running，而会话起起来要 0.2~0.6 秒（首次授权等系统框，本机日志实测 3.7 秒），
    # 300ms 的刷新定时器在这个窗口里每一拍都把勾按回"未勾"。用户再点的那一下按旧目标
    # （not protecting()）算出来还是"开"，等于什么都没做——直到会话真起来勾才出现。
    # 这组用例按真实时序造前提（request_start 之后保持 starting），连续采样勾号。

    def _sample_tick(self, act, seconds=1.2):
        """按 ~50ms 一拍连续采样勾号（§6 第 3 条的"持续观察"用法：量的就是时间轴）。"""
        seen = []
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            QApplication.processEvents()
            seen.append(bool(act.isChecked()))
            time.sleep(0.05)
        self.win._on_ctx_menu_closed()             # discipline-ok(R5): 关菜单的公开入口是 aboutToHide，offscreen 下不保证派发
        return seen

    def test_first_click_tick_survives_the_spin_up(self):
        """点开启 → 勾号必须在一拍刷新内亮起并**一直亮着**，直到会话真起来或起失败。"""
        self.stub.ask_result = "requested"
        self.stub.start_state = STATE_STARTING     # 会话正在起（真机实测 0.2~0.6 秒）
        act = self._trigger_guard_item()
        seen = self._sample_tick(act)
        self.assertTrue(any(seen),
                        "点完开启勾号必须亮起来（用户看到的就是这一下），实测序列=%r" % (seen,))
        first_on = seen.index(True)
        self.assertTrue(all(seen[first_on:]),
                        "勾号亮起之后不许再被刷灭——出现 False 的那一拍就是用户看到的"
                        "「第一次点击没反应」，实测序列=%r" % (seen,))

    def test_second_click_during_spin_up_is_off_not_on_again(self):
        """启动窗口期内（勾已亮、保护还没跑起来）再点一下：必须是「关」，不是「再开一次」。

        勾号亮着的时候，用户点它的唯一合理解读是"我要关"。旧目标取
        `not protecting()`，在这个窗口里算出来永远是"开"——点几下都是开。
        """
        self.stub.ask_result = "requested"
        self.stub.start_state = STATE_STARTING
        act = self._trigger_guard_item()
        self.assertTrue(self._state()["enabled_by_user"], "前置：第一下点完应是开启意图")
        act.trigger()
        d = self._state()
        self.assertFalse(d["enabled_by_user"],
                         "勾亮着的时候再点必须是关，got %r" % (d,))
        self.win._refresh_face_guard_menu_check()  # discipline-ok(R5): 定时器回调，等真 300ms 会给用例引入抖动
        self.assertFalse(act.isChecked(), "关了之后勾号必须灭")
        self.win._on_ctx_menu_closed()             # discipline-ok(R5): 同上

    def test_tick_goes_back_off_when_the_start_fails(self):
        """诚实锁：起不来（权限被拒）时勾号必须及时灭掉 —— "启动也算开"不许变成说谎。"""
        self.stub.ask_result = "requested"
        self.stub.start_state = STATE_DENIED       # 系统里被明确拒绝过
        act = self._trigger_guard_item()
        seen = self._sample_tick(act, seconds=0.8)
        self.assertFalse(all(seen),
                         "起不来的现场勾号必须灭（一直亮着=骗人），实测序列=%r" % (seen,))


    def test_screen_locked_pauses_camera_and_wakes_up(self):
        """v1.4.56 省电：屏幕锁着/显示器休眠时停摄，唤醒后自动继续看守。

        方向必须是"只在系统明确回答锁了才停"：误停 = 用户回来时保护不在，
        比多耗电严重得多（下面那条 fail-open 用例守的就是这个方向）。
        """
        self._enable()
        self.assertTrue(self._state()["camera_held"], "前置：看守应当已在跑")
        self.track_patch(patch.object(sf_presence, "screen_unavailable", lambda: True))
        self.win._face_guard_screen_probe_at = 0.0      # discipline-ok(R5): 探测节流时刻没有对外写入口
        self._poll(1)
        d = self._state()
        self.assertTrue(d["screen_off"], "系统说锁着了，状态必须写清是屏幕暂停")
        self.assertFalse(d["camera_held"], "屏幕锁着时摄像头应当停下（省电）")
        self.assertGreaterEqual(self.stub.stopped, 1)
        # 唤醒：节流到点 + 系统回答"没锁" → 自动重新武装
        self.track_patch(patch.object(sf_presence, "screen_unavailable", lambda: False))
        self.win._face_guard_screen_probe_at = 0.0      # discipline-ok(R5): 同上
        self.win._face_guard_next_retry = 0.0           # discipline-ok(R5): 退避时刻没有对外写入口
        self._poll(2)
        self.assertFalse(self._state()["screen_off"])
        self.assertTrue(self._state()["camera_held"], "唤醒后必须自动继续看守，不用用户再点一次")

    def test_screen_off_leaves_the_polling_timer_running(self):
        """v1.4.60（复核 F2）：停摄像头的同时，不许把"解锁后重新武装"的驱动一起停掉。

        修复前锁屏那一次 `_stop_face_guard` 连 `_guard_timer` 一起停，而全仓唯一会
        重新 start 它的地方（自愈分支与 `_sync_face_guard`）要么就在这张表的回调里、
        要么得用户手动开关菜单 —— 于是**每天必然发生的一次锁屏，换来一整天没有自动
        隐藏**，菜单勾还照着"未在保护"说实话，用户完全看不出发生过什么。
        上一轮用例直调 `_on_face_guard_tick()`（避开 QTimer 抖动），恰好把这一层整个
        测不到：逻辑对、驱动没了，回归照样全绿。所以这里判的是**表的活性**。

        为什么必须看内部状态：QTimer 的 isActive() 没有任何对外读取口，而本轮要钉的
        就是"这张表还在不在跑"；诊断包只说 camera_held，说不出驱动有没有被停走。
        """
        self._enable()
        self.assertTrue(self.win._guard_timer.isActive(),             # discipline-ok(R5): 被测对象就是这张 QTimer
                        "前置：守卫开启后轮询表应当在跑")
        self.track_patch(patch.object(sf_presence, "screen_unavailable", lambda: True))
        self.win._face_guard_screen_probe_at = 0.0                    # discipline-ok(R5): 探测节流时刻没有对外写入口
        self._poll(1)
        self.assertFalse(self._state()["camera_held"], "锁屏时摄像头应当停下（省电）")
        self.assertTrue(self.win._guard_timer.isActive(),             # discipline-ok(R5): 本轮的判据本体
                        "锁屏把轮询表一起停了 —— 解锁后没有任何东西能把守卫重新武装")
        # 解锁：不等用户动手，靠这张还活着的表自己爬起来
        self.track_patch(patch.object(sf_presence, "screen_unavailable", lambda: False))
        self.win._face_guard_screen_probe_at = 0.0                    # discipline-ok(R5): 同上
        self.win._face_guard_next_retry = 0.0                         # discipline-ok(R5): 退避时刻没有对外写入口
        self._poll(2)
        self.assertTrue(self._state()["camera_held"], "唤醒后必须自动继续看守，不用用户再点一次")

    def test_screen_probe_failure_keeps_protection(self):
        """探测抛错只能当作「没锁」：保护不许因为一次系统查询失败就静默停掉。"""
        self._enable()

        def _boom():
            raise OSError("CoreGraphics 不可用")

        self.track_patch(patch.object(sf_presence, "screen_unavailable", _boom))
        self.win._face_guard_screen_probe_at = 0.0      # discipline-ok(R5): 同上
        self._poll(2)
        d = self._state()
        self.assertFalse(d["screen_off"], "读不到答案时不许把状态写成屏幕暂停")
        self.assertTrue(d["camera_held"], "读不到答案时摄像头必须继续跑")
        self.assertIn("运行中", self.win.face_guard_status_text(d),
                      "fail-open 之后状态句仍要说真话")

    def test_toggle_persistence_failure_reverts_menu_and_does_not_sync(self):
        """配置写失败时运行态与持久化意图分离，菜单必须回到旧值且不启动守卫。

        夹具必须复刻 Qt 真实点击的两步：先翻转勾态、再发 triggered —— 只调
        回调而不翻勾态的话，勾态在断言两侧同值，产品漏回滚也测不出来
        （实测：删掉产品里的 setChecked(prev) 后旧夹具照样全绿）。
        """
        action = self._menu_guard_item()
        self.assertFalse(action.isChecked())
        self.win._face_guard_pref = False  # discipline-ok(R5): preference is the private state under test
        self.win._face_guard_monitor = None  # discipline-ok(R5): verify failed persistence does not start monitor
        with patch.object(m, "save_face_guard_pref", return_value=False), \
                patch.object(self.win, "_sync_face_guard") as sync:
            action.setChecked(True)  # Qt 点击可勾选菜单项：先翻勾态
            self.win._on_face_guard_toggled(True, action=action)  # discipline-ok(R5): callback under test
        self.assertFalse(self.win._face_guard_pref)  # discipline-ok(R5): failed save must preserve preference
        self.assertFalse(action.isChecked())
        self.assertIsNone(self.win._face_guard_monitor)  # discipline-ok(R5): failed save must not start monitor
        sync.assert_not_called()
        self.assertEqual(len(self.notices), 1)
        self.assertEqual(self.notices[0][0], "设置没能保存")

    def test_toggle_persistence_failure_when_turning_off_restores_checked_state(self):
        """关闭失败同理：勾态先被 Qt 翻成未勾，保存失败必须翻回选中。"""
        action = self._menu_guard_item()
        action.setChecked(True)
        self.win._face_guard_pref = True  # discipline-ok(R5): preference is the private state under test
        with patch.object(m, "save_face_guard_pref", return_value=False), \
                patch.object(self.win, "_sync_face_guard") as sync:
            action.setChecked(False)  # Qt 点击可勾选菜单项：先翻勾态
            self.win._on_face_guard_toggled(False, action=action)  # discipline-ok(R5): callback under test
        self.assertTrue(self.win._face_guard_pref)  # discipline-ok(R5): failed save must preserve preference
        self.assertTrue(action.isChecked())
        sync.assert_not_called()

    def test_toggle_persists_pref_and_never_pops_a_dialog(self):
        """开→关→再开：偏好必须落盘，而**一个框都不许弹**（v1.4.52 用户要求）。

        原来这条断言的是"代价说明只弹一次"。用户 2026-10-09 看着那个框说
        "去掉这个弹窗提示，永不出现"，所以判据反过来：开启路径上 `notices` 必须为空。
        计数走 setUp 里那层"拦掉真弹窗"的 patch —— 拦的是真 NSAlert，不影响数次数。
        """
        self._trigger_guard_item()          # 开
        self._trigger_guard_item()          # 关
        self._trigger_guard_item()          # 再开
        self.assertTrue(self._state()["enabled_by_user"])
        with open(self.cfg, encoding="utf-8") as f:
            saved = json.load(f)
        self.assertTrue(saved[sf_config.FACE_GUARD_CONFIG_KEY],
                        "菜单点完必须落到配置里，否则重启就回到关闭")
        self.assertEqual(self.notices, [],
                         "开启人脸守卫不许弹任何说明框：" + repr(self.notices))

    def test_toggle_off_stops_camera_immediately(self):
        self._enable()
        self._trigger_guard_item()          # 关闭
        self.assertEqual(self.stub.stopped, 1,
                         "关掉功能的那一刻摄像头必须停（绿灯要灭）")
        self.assertFalse(self._state()["camera_held"])

    def test_hide_event_minimizes_once_and_keeps_camera_watching(self):
        """走真最小化路径：因第二张脸收起时，摄像头不能跟着停。"""
        self._enable()
        self.stub.faces = 2
        self._poll(self._state()["hide_hits"])
        self.assertFalse(self.win.isVisible(), "连续命中应把窗收到 Dock")
        d = self._state()
        self.assertTrue(d["hidden_by_guard"])
        self.assertEqual(self.stub.stopped, 0,
                         "被守卫收起时必须继续看守，否则自动恢复没有数据来源")
        self.assertTrue(d["camera_held"])

    def test_manual_minimize_stops_camera(self):
        self._enable()
        self.win.minimize_to_dock()
        self.assertEqual(self.stub.stopped, 1,
                         "用户自己收的窗不需要自动恢复，摄像头立刻停（绿灯随之灭）")
        self.assertFalse(self._state()["camera_held"])

    def test_guard_hide_then_single_face_restores(self):
        """完整闭环：两张脸收起 → 只剩一张脸满次数 → 自己回到屏幕上。"""
        self._enable()
        self.stub.faces = 2
        self._poll(self._state()["hide_hits"])
        self.assertFalse(self.win.isVisible())
        self.stub.faces = 1
        self.win.running = True        # _restore_window 拒绝恢复"非运行中"的窗口
        self._poll(self._state()["restore_hits"])
        self.assertTrue(self.wait_for(lambda: self.win.isVisible(), timeout=3.0),
                        "只剩一张脸应自动恢复窗口")
        self.assertFalse(self._state()["hidden_by_guard"])

    def test_restore_window_rearms_guard(self):
        self._enable()
        self.stub.stopped = 0
        self.win.minimize_to_dock()
        self.assertFalse(self._state()["camera_held"])
        self.win.running = True
        with patch.object(self.win, "_poll"), patch.object(self.win, "_force_fetch"):
            self.win._restore_window()                     # discipline-ok(R5): Dock 点击恢复的唯一入口，公开侧只有 AppKit reopen 回调
        self.assertTrue(self._state()["camera_held"], "窗口重新可见后守卫要重新武装")

    def test_exit_stops_camera(self):
        self._enable()
        with patch.object(m, "save_config", lambda *a, **k: True):
            self.win._persist_on_exit()                    # discipline-ok(R5): 退出收尾的公开路径是 close()，offscreen 下不保证同步派发 closeEvent
        self.assertGreaterEqual(self.stub.stopped, 1,
                                "退出必须停采集：不能出现「程序退了、绿灯还亮着」")

    def test_disabled_kill_switch_removes_menu_item(self):
        with patch.object(m, "feature_enabled",
                          lambda name: name != "face_guard"):
            texts = self._menu_action_texts()
            self.win._sync_face_guard("test")              # discipline-ok(R5): kill switch 关掉时不该有任何采集对象被创建
        self.assertNotIn(GUARD_MENU_TEXT, texts)
        self.assertFalse(self._state()["camera_held"],
                         "kill switch 关掉后不得留下任何采集对象")

    def _menu_guard_item(self):
        ev = QContextMenuEvent(QContextMenuEvent.Reason.Mouse,
                               QPoint(10, 10), QPoint(10, 10))
        self.win.contextMenuEvent(ev)
        hit = [a for a in self._menu_actions() if a.text() == GUARD_MENU_TEXT]
        self.assertTrue(hit, "右键菜单里没有人脸守卫项")
        return hit[0]

    # ---- v1.4.50「未授权死路」：勾选口径 = 此刻在不在保护 ----
    def test_menu_check_reflects_protection_not_the_setting(self):
        """实机取证：配置开着 + 没授权 = 勾是打的、摄像头从没打开过。

        旧口径 `setChecked(bool(pref))` 让用户看到"已开启"，而要点**两次**
        （先关再开）才可能弹授权框 —— 第二个人入镜自然不会收窗。
        """
        self.stub.start_state = sf_presence.STATE_NEEDS_AUTH
        self._enable()
        self.assertEqual(self._state()["state"], sf_presence.STATE_NEEDS_AUTH)
        self.assertFalse(self._menu_guard_item().isChecked(),
                         "保护没在跑时勾必须灭掉，否则界面就是在说假话")
        self.assertTrue(self._state()["enabled_by_user"],
                        "勾灭不等于偏好丢失：配置里仍应是开启，重启后继续重试")

    def test_one_click_from_needs_auth_asks_permission(self):
        """一次点击就要发起授权，不能要求"先关再开"。"""
        self.stub.start_state = sf_presence.STATE_NEEDS_AUTH
        self._enable()                        # 这一次点击已经问过（asks=1）
        before = self.stub.asks
        self._menu_guard_item().trigger()     # 用户照着状态行再点一次
        self.assertEqual(self.stub.asks, before + 1,
                         "没在保护时再点一次必须再发起授权请求")
        self.assertTrue(self._state()["enabled_by_user"], "这次点击不许把偏好写成关闭")

    def test_running_menu_check_is_on_and_click_turns_it_off(self):
        """反过来也必须成立：真在保护时勾是亮的，点一下就关（停摄像头）。"""
        self._enable()
        self.assertTrue(self._menu_guard_item().isChecked())
        self.stub.stopped = 0
        self._menu_guard_item().trigger()
        self.assertEqual(self.stub.stopped, 1, "在跑的时候点一下应当关掉")
        self.assertFalse(self._state()["enabled_by_user"])

    def test_needs_auth_line_points_at_the_real_menu_item(self):
        """状态行指的路必须是屏幕上真实存在的那一项。

        旧文案写「人脸在场守卫」，而菜单项当时叫「有第二人时自动收起」（现名见
        `sf_constants.FACE_GUARD_MENU_TEXT`）—— 用户照着
        提示去找一个不存在的项，未授权就成了出不去的死路（台账 E02 复发的一环）。
        """
        self.stub.start_state = sf_presence.STATE_NEEDS_AUTH
        self._enable()
        line = self._status_line()
        self.assertIn("未授权", line, "没授权时必须说清「保护没在跑」")
        self.assertIn(GUARD_MENU_TEXT, line,
                      "提示必须指着真实菜单项文案，got %r" % line)

    def test_detect_errors_are_showed_instead_of_running(self):
        """帧来了却一次都没数成时，状态不许再写"运行中"。"""
        self._enable()
        self.stub.frames = 0
        self.stub.detect_errors = 7
        line = self._status_line()
        self.assertIn("认不出脸", line, "检测连续失败必须写清楚，got %r" % line)
        self.assertNotIn("运行中", line, "认不出脸的时候不能挂着「运行中」")
        self.assertIn("7", line, "次数要说出来，不能说「有点问题」")

    def test_stalled_while_guard_hidden_still_self_heals(self):
        """复核 F1：中断发生在"窗口正被守卫收着"期间时，30 秒后必须真的重新武装。

        这是最坏的组合：两人入镜自动收起 → 摄像头被别的程序抢走 → 判定中断。
        停表时若把"因第二张脸被收起"一起抹掉，重试就会被判成"用户自己收的窗、
        不用看守"而再次停表 —— 自动恢复从此死掉，而菜单写的还是"等它松开"。
        """
        self._enable()
        self.stub.faces = 2
        self._poll(self._state()["hide_hits"])
        self.assertFalse(self.win.isVisible())
        self.stub.snapshot = lambda: (2, _long_ago())
        with patch.object(m, "FACE_GUARD_STALE_SEC", 0.0):
            self._poll(1)
        d = self._state()
        self.assertTrue(d["stalled"], "采不到画面应判中断")
        self.assertTrue(d["hidden_by_guard"], "中断不能把「因第二张脸收起」一起抹掉")
        # 退避到点（把 30 秒等待跳过）：拿到新帧后应重新武装并撤掉中断标记。
        # 两拍：第 1 拍重新建会话（本拍只做同步），第 2 拍才吃到新帧并撤中断标记。
        self.win._face_guard_next_retry = 0.0        # discipline-ok(R5): 退避时刻没有对外写入口
        self.stub.snapshot = lambda: (1, time.monotonic())
        self._poll(2)
        self.assertTrue(self._state()["camera_held"], "重试必须真的把采集重新拉起来")
        self.assertFalse(self._state()["stalled"], "拿到新帧后不能再挂着「已中断」")

    def test_zero_frames_never_arrived_declares_broken(self):
        """复核 F2：会话起来了却一帧都没来过，也要判中断，不能永远显示"运行中"。

        依据是实测事实：设备被占用/未授权时 startRunning 照样返回"在跑"但不发帧，
        所以"最后一帧的时刻"一直是 0 —— 只看它就等于这条超时判定根本不参与。
        """
        self._enable()
        self.stub.faces = 0
        self.stub.snapshot = lambda: (0, 0.0)
        self.stub.running_since = _long_ago()
        with patch.object(m, "FACE_GUARD_STALE_SEC", 0.0):
            self._poll(1)
        self.assertTrue(self._state()["stalled"], "零帧必须按同样口径判中断")
        self.assertGreaterEqual(self.stub.stopped, 1, "判中断要松开摄像头")


    def test_stale_sample_does_not_decide(self):
        """采不到新帧（摄像头被抢走）时，不能拿一分钟前的「一个人」触发恢复。"""
        self._enable()
        self.stub.faces = 2
        self._poll(self._state()["hide_hits"])
        self.assertTrue(self._state()["hidden_by_guard"])
        self.stub.faces = 1
        self.stub.snapshot = lambda: (1, _long_ago())   # 一分钟前的旧样本
        self._poll(self._state()["restore_hits"] * 2)
        self.assertTrue(self._state()["hidden_by_guard"],
                        "过期样本一次都不该喂进判定，更不该凑满恢复次数")


    # ---- v1.4.41 复核项的回归用例 ----
    def _stall_the_guard(self):
        """让守卫进入"采不到画面"的中断态：先拿一拍新帧，之后快照永远停在旧时刻。"""
        self._enable()
        self.stub.faces = 1
        self._poll(1)
        self.stub.snapshot = lambda: (1, _long_ago())
        # 常量由主窗直取（sf_constants 是唯一来源），patch 必须打在查找处
        with patch.object(m, "FACE_GUARD_STALE_SEC", 0.0):
            self._poll(1)
        return self._state()

    def test_manual_dock_restore_clears_guard_state(self):
        """复核 F1：自动收起后用户自己点 Dock 图标恢复，"因第二张脸收起"必须一起结掉。

        残留的后果不是省电那么轻：下一次他手动缩小窗口时摄像头不停，约 5 秒后
        程序会自己把窗口弹回来 —— 等于推翻用户刚做过的操作。
        """
        self._enable()
        self.stub.faces = 2
        self._poll(self._state()["hide_hits"])
        self.assertFalse(self.win.isVisible())
        self.assertTrue(self._state()["hidden_by_guard"])
        self.win.running = True
        with patch.object(self.win, "_poll"), patch.object(self.win, "_force_fetch"):
            self.win._restore_window()                     # discipline-ok(R5): Dock 点击恢复的唯一入口
        d = self._state()
        self.assertFalse(d["hidden_by_guard"], "手动恢复后不该残留「因第二张脸收起」")
        self.assertTrue(d["camera_held"], "窗口重新可见，守卫应继续武装")
        self.stub.stopped = 0
        self.win.minimize_to_dock()
        self.assertEqual(self.stub.stopped, 1, "这次手动缩小必须真的停摄像头")
        self.assertFalse(self._state()["camera_held"])

    def test_menu_enable_is_the_only_permission_ask_point(self):
        """v1.4.47 的用户要求落成断言：只有**点开启**那一步能弹系统框。

        其余路径（节拍轮询、缩小、恢复、状态同步）都不许发起授权请求 —— 以前正是
        轮询路径会替用户弹框，而产物 ad-hoc 签名每次更新后状态都退回 undetermined，
        表现就是"更新完一打开就又弹一次"。
        """
        self._enable()
        self.assertEqual(self.stub.asks, 1, "点开启应当恰好发起一次授权请求")
        self._poll(3)
        self.win.minimize_to_dock()
        self._poll(2)
        with patch.object(self.win, "_force_fetch"):
            self.win._restore_window()          # discipline-ok(R5): Dock 恢复是弹框的高发路径，只能走这个内部入口
        self.win._sync_face_guard("tick")       # discipline-ok(R5): 同步的内部触发路径，公开入口只有菜单点击
        self.assertEqual(self.stub.asks, 1, "除用户主动开启外，任何路径都不许弹系统授权框")

    def test_no_frames_declares_guard_broken(self):
        """复核 F2：长时间一帧都不来 = 保护已中断，要停手并如实显示，不能空转点灯。"""
        d = self._stall_the_guard()
        self.assertTrue(d["stalled"], "采不到画面必须判定为中断")
        self.assertGreaterEqual(self.stub.stopped, 1, "判定中断要把摄像头停掉")
        self.assertFalse(d["camera_held"])

    def test_stalled_status_never_claims_running(self):
        self._stall_the_guard()
        line = self._status_line()
        self.assertIn("保护已中断", line, "中断时状态要写「保护已中断」：" + line)
        self.assertNotIn("运行中", line,
                         "不能再写「运行中」—— 那是骗人在有保护")

    def test_window_hidden_by_other_path_stops_camera(self):
        """复核 F3：窗口以「缩小」以外的路径下屏（系统隐藏等），摄像头不能一直开着。"""
        self._enable()
        self.assertTrue(self.win.isVisible())
        self.win.hide()                    # 故意不走 minimize_to_dock
        self._poll(1)
        self.assertGreaterEqual(self.stub.stopped, 1,
                                "不在屏上就该停采集，否则用户选的「只在可见时检测」落空")
        self.assertFalse(self._state()["camera_held"])

    def test_denied_permission_sends_the_click_to_system_settings(self):
        """权限被明确**拒绝**过之后，那一下点击必须有去处（v1.4.59 用户拍板）。

        macOS 在 denied 之后永远不会再弹授权框，所以菜单再点就是空转 ——
        与 v1.4.57 修的"没答就永久装死"是同一枚症状的另一半。
        这里不弹任何解释框（本 app 的规矩），直接把用户带到系统设置的摄像头页。
        """
        self.stub.ask_result = "denied"
        opened = []
        with patch.object(m.FloatWindow, "_open_camera_privacy_settings",
                          lambda self: opened.append(1)):
            self._enable()
        self.assertEqual(opened, [1], "被拒之后点「自动隐藏窗口」必须打开系统摄像头设置页")

    def test_not_determined_click_does_not_open_settings(self):
        """还没答的走系统弹框，不许顺手打开系统设置（那是另一条路）。"""
        opened = []
        with patch.object(m.FloatWindow, "_open_camera_privacy_settings",
                          lambda self: opened.append(1)):
            self._enable()
        self.assertEqual(opened, [], "requested 这一档只该出系统授权框")

    def test_interactive_stop_does_not_wait_for_the_lock(self):
        """复核 F5：用户点缩小这条路径不许在主线程等会话锁（实测数百毫秒）。"""
        self._enable()
        self.win.minimize_to_dock()
        self.assertIn(False, self.stub.stop_waits,
                      "交互路径要用 stop(wait=False)，界面才不会卡半秒")

    def test_exit_stop_waits_for_the_real_teardown(self):
        """反过来：退出收尾必须同步等完，否则进程走了、绿灯还亮着。"""
        self._enable()
        with patch.object(m, "save_config", lambda *a, **k: True):
            self.win._persist_on_exit()                    # discipline-ok(R5): 退出收尾的公开路径是 close()，offscreen 下不保证同步派发 closeEvent
        self.assertIn(True, self.stub.stop_waits, "退出必须真把会话停掉")

    def test_unfinished_stop_is_replayed_at_exit(self):
        """S1 第二处：`stop(wait=False)` 返回 False 是"**还没停**"，不许就地丢弃。

        补停跑在 daemon 线程上，进程一退就被硬杀 → 会话永远没停、菜单栏绿灯不灭，
        而诊断包那时会报 camera_held=false（说谎）。所以未完成的停要挂账，
        退出收口必须再补一次 wait=True。
        """
        self._enable()
        self.stub.stop_defers = True
        self.win.minimize_to_dock()                  # 交互路径：stop(wait=False) → False
        self.assertEqual(self._state()["pending_stops"], 1,
                         "没停完的 monitor 必须留名，否则退出时没人补停")
        self.assertTrue(self._state()["camera_held"],
                        "还没停完就说 camera_held=false —— 诊断包替故障撒谎")
        self.stub.stop_waits.clear()
        with patch.object(m, "save_config", lambda *a, **k: True):
            self.win._persist_on_exit()                    # discipline-ok(R5): 同上，退出路径没有公开同步入口
        self.assertIn(True, self.stub.stop_waits,
                      "退出收口必须把挂起的那次停兑现（wait=True）")
        self.assertEqual(self._state()["pending_stops"], 0, "兑现后要销账")

    def test_denied_state_is_named_in_diagnostics(self):
        """复核 F11：授权被拒这种"看着开着其实没跑"的状态，必须有一句话说清。"""
        self.stub.start_state = STATE_DENIED
        self.stub.last_error = "摄像头权限为 denied"
        self._trigger_guard_item()          # 走公开勾选路径，不直接改偏好
        line = self._status_line()
        self.assertIn("保护未生效", line)
        self.assertIn("摄像头权限", line, line)

    def test_paused_state_is_named_in_diagnostics(self):
        self._enable()
        self.win._face_guard_paused = True                 # discipline-ok(R5): 暂停只由内部看守超时置位，用例直接构造该状态
        self.assertIn("已暂停", self._status_line())


class TestZeroFrameWindowAndFriendlyErrors(TestWindowWiring):
    """v1.4.45 复核 A2：零帧窗口期说实话，并提前判中断（60 秒留给"来过帧"的会话）。"""

    def test_zero_frame_window_reports_honestly(self):
        self._enable()
        self.stub.snapshot = lambda: (0, 0.0)                 # 一帧都没来过
        self.stub.running_since = _long_ago(3.0)      # 刚起 3 秒，还在窗口期内
        line = self._status_line()
        self.assertIn("尚未收到画面", line,
                      "等画面的窗口期要把实话说出来：" + line)
        self.assertNotIn("运行中 ·", line,
                         "还没数过一张脸就不能写「运行中」：" + line)

    def test_zero_frame_is_declared_stalled_after_short_budget(self):
        self._enable()
        self.stub.snapshot = lambda: (0, 0.0)
        self.stub.running_since = _long_ago(m.FACE_GUARD_FIRST_FRAME_SEC + 0.5)
        self._poll(1)
        d = self._state()
        self.assertTrue(d["stalled"], "零帧超过短预算就该判中断，不必等满 60 秒")
        self.assertGreaterEqual(self.stub.stopped, 1, "判中断要真的停摄像头")

    def test_frames_history_keeps_the_60s_budget(self):
        """来过帧的会话仍走 60 秒宽容期：偶发卡一拍不该被零帧短预算误杀。"""
        self._enable()
        self.stub.faces = 1
        self._poll(1)
        self.stub.snapshot = lambda: (1, _long_ago())
        self._poll(1)
        self.assertFalse(self._state()["stalled"],
                         "有帧历史的会话不该被零帧短预算误杀")
        self.assertTrue(self._state()["camera_held"])


class TestFriendlyFailureReason(SFCase):
    """复核 A3：菜单与诊断包里的失败原因必须是人话，repr 只进日志。"""

    def test_block_signature_error_becomes_plain_chinese(self):
        exc = TypeError("Argument 3 is a block, but no signature available")
        text = sf_presence._friendly_error(exc)               # discipline-ok(R5): 被测函数是模块私有实现
        self.assertIn("缺少必要组件", text)
        self.assertNotIn("TypeError", text)

    def test_our_own_runtime_errors_pass_through(self):
        text = sf_presence._friendly_error(                   # discipline-ok(R5): 同上
            RuntimeError("摄像头输入通道建立失败（设备可能被别的进程占用）"))
        self.assertIn("摄像头输入通道", text)

    def test_unknown_error_does_not_leak_its_text_to_the_ui(self):
        class _Weird(Exception):
            pass

        text = sf_presence._friendly_error(_Weird("x=1; drop table users"))  # discipline-ok(R5): 被测函数是模块私有实现
        self.assertNotIn("drop table", text, "识别不了的异常原文不能上界面")
        self.assertTrue(text.endswith("写入日志）"), "但要告诉用户原文在哪里可查")

    def test_session_failure_shows_friendly_reason(self):
        """起会话失败时，monitor.last_error 必须已经是人话（它会被菜单直读）。"""

        class _NoneSession:
            # 真代码是 `alloc().init()` 两连发，桩要走到"init 返回 None"才踩得中
            # 产品里那句 `AVCaptureSession 构造失败` 的 RuntimeError。
            @classmethod
            def alloc(cls):
                return object.__new__(cls)

            def init(self):
                return None

        self.track_patch(patch.object(sf_presence, "_load_frameworks",
                                      return_value=({"AVCaptureDevice": object(),
                                                     "AVCaptureSession": _NoneSession},
                                                    "")))
        self.track_patch(patch.object(sf_presence, "_camera_devices",
                                      return_value=[object()]))
        self.track_patch(patch.object(sf_presence, "camera_access_status",
                                      return_value="authorized"))
        mon = FacePresenceMonitor()
        mon.start()
        self.assertIn("构造失败", mon.last_error, "我们自己抛的中文要原样可读")
        self.assertNotIn("RuntimeError", mon.last_error, "异常类名不该出现在界面文案里")


class TestEdgeAndDistanceRules(SFCase):
    """2026-10-12 用户拍板的两条口径：贴边被切一半的脸也算第二个人；
    站在 1.5 米外的人不算第二个人。全部走纯函数，不需要框架也不需要摄像头。

    数值依据（真 Vision 实测，复现入口 `tools/verify_face_presence.py --edge` / `--near`）：
      半张脸出画 0.587~0.638（暗光那侧正好掉在旧阈值 0.6 之下 → 原来不收窗）
      1.5 米那一档 ≈ 画面宽 6.5%，把握分仍有 0.78~0.83（所以两条闸门不打架）
      3% 以下系统根本不返回那张脸，阈值救不了 —— 不参与判定
    """

    def test_border_face_below_the_main_bar_still_counts(self):
        """用户拍板第一条：贴在画面边界、把握分只有 0.57 的脸要算数。"""
        self.assertEqual(count_faces([_ObsBox(0.57, x=0.70, w=0.30)]), 1,
                         "半张脸出画的人又回到不收窗了 —— 贴边这条放宽没了")

    def test_relaxation_does_not_leak_to_the_middle_of_the_frame(self):
        """同样的 0.57 出现在画面中间不能算：否则就是全局降到 0.55。"""
        self.assertEqual(count_faces([_ObsBox(0.57, x=0.30, w=0.30)]), 0,
                         "放宽溢出到画面中央的框：海报与屏幕里的人像会更容易误收窗")

    def test_border_face_below_the_edge_bar_is_still_rejected(self):
        self.assertEqual(count_faces([_ObsBox(0.40, x=0.70, w=0.30)]), 0,
                         "贴边就一律计入的话，噪点框也能把窗收掉")

    def test_border_box_gets_a_discounted_distance_gate(self):
        """贴边的框按 0.61 折算后再比尺寸闸门 —— 不是免检，也不是原样要求。

        实测依据：同一张脸完整时框宽 233px，切掉一半只剩 144px（0.61 倍），
        而且**高也跟着一起缩**，所以没有任何维度能在被切时反推真实大小。
        要求原尺寸就会把"站在旁边但半张脸出画的人"当成很远的人滤掉（推翻 R1）；
        完全免检则会把"3 米外正好卡在边角的人"算进来（推翻 R2）。
        """
        self.assertEqual(count_faces([_ObsBox(0.80, x=0.96, w=0.05)]), 1,
                         "被切短的近脸被尺寸闸门误杀：站在旁边的人反而不算了")
        self.assertEqual(count_faces([_ObsBox(0.80, x=0.98, w=0.02)]), 0,
                         "贴边就完全免检：远处卡在边角的人又被算进来了（R2 失守）")

    def test_far_face_in_the_middle_does_not_count(self):
        """用户拍板第二条：完整在画面里、但只占 4% 画面宽（比 1.5 米还远）的不算。"""
        self.assertEqual(count_faces([_ObsBox(0.80, x=0.30, w=0.04)]), 0,
                         "远处的人把窗收掉了 —— 他根本看不清屏幕上的价")

    def test_near_face_in_the_middle_counts(self):
        self.assertEqual(count_faces([_ObsBox(0.80, x=0.30, w=0.20)]), 1)

    def test_unreadable_geometry_never_gets_the_relaxation(self):
        """读不到 boundingBox 时不许白拿放宽，但也不取消资格 —— 两个方向都钉住。

        为什么这么分：放宽（0.55 / 免尺寸）必须建立在"确实看到框贴着边"这个事实上，
        几何读不出来就自动放宽，等于给桥接形状事故开后门（v1.4.49 建会话、v1.4.57
        的 reopen 类都栽在"形状悄悄变了"上）。而**取消**资格是另一个方向的事：
        漏掉一个人的代价高于多收一次窗，所以把握分本身够 0.6 的框照样计入。
        """
        self.assertEqual(count_faces([_ObsBox(0.57, x=0.70, w=0.30, geo=False)]), 0,
                         "读不到几何却按贴边放宽计入：等于给形状事故开后门")
        self.assertEqual(count_faces([_ObsBox(0.80, x=0.70, w=0.30, geo=False)]), 1,
                         "读不到几何就取消资格：形状一变就从'少算'变成'不算'，是漏检方向")

    def test_owner_plus_far_guest_does_not_hide(self):
        """两张脸都在，但第二张在 1.5 米外 → 计数只有 1 → 迟滞不推进、不收窗。

        这是"算几张脸"与"该不该收窗"之间真正的接缝，只测 count_faces 测不到。
        """
        faces = [_ObsBox(0.85, x=0.05, w=0.30), _ObsBox(0.80, x=0.40, w=0.04)]
        a = PresenceArbiter()
        events = [a.feed(count_faces(faces), now=float(i)) for i in range(6)]
        self.assertNotIn(EVENT_HIDE, events, "远处的人不该触发收窗")

    def test_owner_plus_near_guest_hides(self):
        """两张近脸 + 驻留满 0.38 秒 → 收窗。差 0.1 秒都不许动。"""
        faces = [_ObsBox(0.85, x=0.05, w=0.30), _ObsBox(0.80, x=0.40, w=0.10)]
        a = PresenceArbiter()
        self.assertEqual(a.feed(count_faces(faces), now=0.0), EVENT_NONE)
        self.assertEqual(a.feed(count_faces(faces), now=0.25), EVENT_NONE,
                         "连击满了但驻留没满（0.25 < 0.38）就收窗，驻留这条白加")
        self.assertEqual(a.feed(count_faces(faces), now=0.50), EVENT_HIDE,
                         "驻留满 0.38 秒后必须真的收窗")


class TestDwellAndEventDriven(SFCase):
    """驻留时长与"数完就判"这两条新行为（2026-10-12 二次复核 F1 的落地）。"""

    def test_dwell_is_measured_from_the_first_time_two_faces_are_seen(self):
        a = PresenceArbiter()
        a.feed(2, now=0.0)
        self.assertEqual(a.feed(2, now=0.37), EVENT_NONE, "差一瞬也要等满")
        self.assertEqual(a.feed(2, now=0.38), EVENT_HIDE)

    def test_dwell_restart_after_a_gap(self):
        """中途掉回一张脸，驻留计时必须重新开始 —— 不然闪烁的误检能攒满 0.38 秒。"""
        a = PresenceArbiter()
        a.feed(2, now=0.0)
        a.feed(1, now=0.30)
        self.assertEqual(a.feed(2, now=0.50), EVENT_NONE,
                         "断过一次之后仍按 0.0 起算 = 计时没重置")
        self.assertEqual(a.feed(2, now=0.60), EVENT_NONE,
                         "重连后 0.1 秒就收窗：驻留起点没跟着重来")
        self.assertEqual(a.feed(2, now=0.90), EVENT_HIDE,
                         "从最后一次真正看到两张脸起算满 0.38 秒，才该动手")

    def test_restore_also_waits_for_the_dwell(self):
        a = PresenceArbiter()
        a.feed(2, now=0.0)
        a.feed(2, now=0.40)                       # 已收起
        for i in range(a.restore_hits):
            got = a.feed(1, now=0.5 + i * 0.25)
        self.assertEqual(got, EVENT_RESTORE,
                         "恢复同理反推：连击满且驻留满才把行情亮回来")
        b = PresenceArbiter()
        b.feed(2, now=0.0)
        b.feed(2, now=0.40)
        for i in range(b.restore_hits):
            ev = b.feed(1, now=0.45 + i * 0.02)   # 五次都在 0.38 秒内喂完
        self.assertEqual(ev, EVENT_NONE, "恢复侧的驻留被写丢了")


class TestRealDetectorAgreesWithTheRules(SFCase):
    """上面那些假框把"贴边的脸把握分会掉到 0.55~0.6 之间"当成了前提。

    前提本身只有真 Vision 能证 —— 而假类恰好是这仓反复吃亏的那一类盲区
    （台账 E02/E03：假类返回值不会呈现真实形状）。所以这里起一个子进程跑
    `tools/verify_face_presence.py --edge`，判据是它的退出码。
    人脸素材按隐私决定不入库（.gitignore 里那条 `outputs/人脸守卫自测素材-*.png`），
    所以没有素材的机器/工作树上是**跳过并说明理由**，不是通过。
    """

    def test_edge_curve_on_real_vision_still_counts_half_cropped_face(self):
        import subprocess
        tool = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "tools", "verify_face_presence.py")
        asset = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "outputs", "人脸守卫自测素材-两人照.png")
        if not os.path.exists(asset):
            self.skipTest("人脸素材不在本树（不入库），真 Vision 贴边这一条本轮未核验")
        rc = subprocess.run([sys.executable, tool, "--edge"],
                            capture_output=True, text=True, timeout=300)
        self.assertEqual(rc.returncode, 0,
                         "真 Vision 下被切一半的边脸没被算成第二个人：%s"
                         % (rc.stdout[-500:] + rc.stderr[-300:]))


if __name__ == "__main__":
    unittest.main()
