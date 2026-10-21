# -*- coding: utf-8 -*-
"""人脸在场守卫 v1.4.40 新增 —— 画面里出现第二张脸时，通知主窗收起到 Dock。

分成两层，故意不合并：
  · `PresenceArbiter`：纯决策（迟滞状态机）。不碰 ObjC、不碰 Qt、不碰时间以外的
    外部世界，所以能在没有摄像头的机器上被用例完整驱动。
  · `FacePresenceMonitor`：桥接层。只负责"每 tick 数出几张脸"并上报计数，
    不做任何窗口决策 —— 决策错了会直接改动界面，桥接错了只影响数据来源。

隐私硬约束（改这个文件之前先读这三条）：
  1. 视频帧只在采集回调的调用栈里存在，不写盘、不入网络、不进日志、不进任何
     列表缓存；日志里只允许出现整数计数和状态名。
  2. 这里做的是**人脸检测**（框出有几个），不是**人脸识别**（是谁）。没有模板、
     没有特征向量，因此不存在"把某个人的脸绑进配置"的风险，也解释不了"是谁"。
  3. 摄像头一开，macOS 菜单栏的绿色占用灯就会亮，且**无法隐藏**（系统强制）。
     这个功能做不到"悄悄地看"。v1.4.51/52 之后界面上不再常驻状态行与菜单灰字
     （用户口径），所以"保护到底在不在跑"的落点是：菜单栏那颗绿灯（肉眼）、
     诊断包 `face_guard.json` 的 `status_text`、以及 debug.log —— 不是界面上的一行字。
     这句话此前写的是"见主窗右键菜单的状态行"，那一行早就被撤掉了（2026-10-12 复核改）。

为什么用系统 Vision，不引入 OpenCV / MediaPipe（实测依据）：
  · 不塞模型、不加二进制框架：Vision 与 AVFoundation 都是 macOS 自带的系统框架。
    代价是多三个纯 Python 的 PyObjC 桥接包（本机实测 site-packages 合计约 1.6MB，
    见 requirements.txt）—— 这三个不是可选装饰：授权接口的第三个参数是 ObjC block，
    只装 pyobjc-core 时拿不到方法元数据就调不动，三条绕法实测全部失败
    （细节写在 `_request_access` 的注释里，别再看一眼就把它当冗余依赖删了）。
    类本身仍用 `objc.lookUpClass` 动态取：本机实测 7 个必需类全部解析成功。
  · 单帧实测 18.0ms（本机 1024x768 两人照，冷启动首帧 76.1ms 含框架预热），
    按 1 次/秒 的节拍跑，占空比约 1.8%，比"每帧都检"低两个数量级；
  · OpenCV 的 Haar 级联在同样两张脸上没有这么稳，还要多带 ~40MB 依赖。
"""

import ctypes
import threading
import time
from typing import Any, Dict, Optional, Tuple

from sf_constants import (FACE_GUARD_CAMERA_FPS, FACE_GUARD_CAMERA_PRESET,
                          FACE_GUARD_CONF_MIN, FACE_GUARD_DWELL_SEC,
                          FACE_GUARD_EDGE_CONF_MIN, FACE_GUARD_EDGE_SHRINK,
                          FACE_GUARD_EDGE_TOUCH, FACE_GUARD_EXPEDITE_BUDGET,
                          FACE_GUARD_EXPEDITE_SEC, FACE_GUARD_HIDE_HITS,
                          FACE_GUARD_MAX_IDLE_SEC, FACE_GUARD_MENU_TEXT,
                          FACE_GUARD_NEAR_FACE_W, FACE_GUARD_REASK_COOLDOWN_SEC,
                          FACE_GUARD_RESTORE_HITS, FACE_GUARD_TICK_SEC)
from sf_logging import log_line, log_line_throttled, log_swallow

# ---- 决策输出：调用方只认这三个字符串 ----
EVENT_NONE = ""
EVENT_HIDE = "hide"
EVENT_RESTORE = "restore"
EVENT_IDLE = "idle"          # 自动收起后看守超时：停摄像头，改由手动恢复

# ---- 状态：界面可读取，用于把"保护到底在不在跑"讲清楚 ----
STATE_OFF = "off"
STATE_STARTING = "starting"
STATE_RUNNING = "running"
STATE_DENIED = "denied"      # 用户没给摄像头权限（保护**没有**在跑）
STATE_RESTRICTED = "restricted"   # MDM/家长控制等策略禁止，代码层面无法解除
STATE_NO_CAMERA = "no_camera"
STATE_UNSUPPORTED = "unsupported"  # 框架/类取不到（非 macOS 或系统框架异常）
STATE_FAILED = "failed"      # 会话起不来，原因见 last_error
STATE_NEEDS_AUTH = "needs_auth"  # 还没问过权限，**不是**"开了但没跑"：等用户主动点菜单再问

# AVCaptureMediaType 的字面值。优先用 pyobjc-framework-AVFoundation 里的官方常量
# （本机实测 AVMediaTypeVideo == 'vide'），取不到再退回字面量。
# 注意这个取值是**惰性**的：本模块在启动路径上就被导入，不能因为一个默认关闭的
# 功能而在冷启动时把 AVFoundation 框架拉进进程。
_MEDIA_VIDEO_FALLBACK = "vide"
_MEDIA_VIDEO_CACHED: Optional[str] = None


def _media_video() -> str:
    global _MEDIA_VIDEO_CACHED
    if _MEDIA_VIDEO_CACHED is not None:
        return _MEDIA_VIDEO_CACHED
    try:
        import AVFoundation
        _MEDIA_VIDEO_CACHED = str(AVFoundation.AVMediaTypeVideo)
    except Exception:
        _MEDIA_VIDEO_CACHED = _MEDIA_VIDEO_FALLBACK
    return _MEDIA_VIDEO_CACHED


_GEO_FAILS = 0          # 读不到检测框几何的累计次数（只进诊断包，是个整数）


def geometry_failures() -> int:
    """给诊断包读的整数计数：几何这一路到底有没有悄悄坏过。"""
    return _GEO_FAILS


def _box_rect(ob) -> Optional[Tuple[float, float, float, float]]:
    """检测框的归一化 (x, y, 宽, 高)；读不到返回 None。

    Vision 的框原点在**左下**，且这台机器上实测是正方形（宽恒等于高）。
    读不到几何绝不等于"贴边"或"够近" —— 一律走最严的那条判定，
    否则任何一处桥接形状变化都会自动获得放宽资格。

    但"降级方向保守"不等于"可以静默"（AGENTS §6 第 9 条）：真出事时的表现是
    贴边放宽整体悄悄失效、边缘的人不再触发收窗，界面上一切正常。所以这里
    必须留一条限流日志 + 一个整数计数，否则事后翻日志一个字都查不到。
    """
    global _GEO_FAILS
    try:
        b = ob.boundingBox()
        return (float(b[0][0]), float(b[0][1]), float(b[1][0]), float(b[1][1]))
    except Exception as exc:
        _GEO_FAILS += 1
        log_line_throttled("face_guard.geometry_unreadable",
                           "face_guard 读不到检测框几何（累计 %d 次），"
                           "本帧按最严口径计数：贴边放宽与距离闸门都不生效：%r"
                           % (_GEO_FAILS, exc), level="WARNING", every=30)
        return None


def _touches_edge(r) -> bool:
    """框的任一条边贴到画面边界。

    为什么单独判这一条：人站在镜头边缘时脸多半被切短，而实测**宽和高会一起缩**
    （同一张脸完整时 233px，切掉一半只剩 144px），所以被切的框既拿不到真实尺寸、
    系统给的把握分也系统性偏低（2026-10-12 实测，`tools/verify_face_presence.py --edge`）。
    """
    x, y, w, h = r
    t = FACE_GUARD_EDGE_TOUCH
    return (x <= t or x + w >= 1.0 - t or y <= t or y + h >= 1.0 - t)


def max_face_width(observations) -> float:
    """画面里最大的那张达标脸占画面宽度的比例（没有就返回 0.0）。

    为什么要把这个数留下来：「1.5 米内才算第二个人」这条规矩要用**镜头水平视角**
    才能从"占画面几成"换成米，而这台机器上实测读不到（`videoFieldOfView` 返回 0.0，
    CoreMedia 也没导出取视角的函数）。有了这个比例，再量一次实际坐姿距离就能把视角
    反推出来，阈值就不再是估计值。它是一个纯浮点数，不含任何画面内容（隐私第 1 条）。
    """
    best = 0.0
    for ob in observations or []:
        try:
            conf = float(ob.confidence())
        except Exception:
            continue
        if conf < FACE_GUARD_CONF_MIN:
            continue
        r = _box_rect(ob)
        if r is not None and r[2] > best:
            best = r[2]
    return best


def count_faces(observations, conf_min: float = FACE_GUARD_CONF_MIN,
                edge_conf_min: float = FACE_GUARD_EDGE_CONF_MIN,
                near_w_min: float = FACE_GUARD_NEAR_FACE_W) -> int:
    """把"这一帧检测到几个框"折算成"画面里有几张脸算数"。纯函数，没有 ObjC 也没有 Qt。

    三条判定，按 2026-10-12 用户拍板的口径：

    1. **把握分门槛**：清晰正脸实测 0.82；低于 `conf_min`(0.6) 的框不计。
       误收一次窗的代价是"用户看不出行情为什么消失"，几次之后功能就被关掉，保护归零；
       而真要凑过来看屏幕的人就在旁边，框不会小、把握分也不会低。
       读不到把握分的框**计一张**：漏掉一个人的代价高于多收一次窗。
    2. **贴边的框放宽到 `edge_conf_min`(0.55)** —— 用户明确要求"被切一半的边脸也算
       第二个人"。依据与取值全部写在 `sf_constants.FACE_GUARD_EDGE_CONF_MIN` 旁边。
    3. **框宽还须过距离闸门**（≈站在 1.5 米内才算第二个人；换算与不确定性写在
       `sf_constants.FACE_GUARD_NEAR_FACE_W` 旁边）。贴边的框按
       `FACE_GUARD_EDGE_SHRINK`(0.61) 打折后再比 —— 不是免检：被边界切掉一半时
       实测框宽只剩 0.61 倍，不打这个折就会把"站在旁边但半张脸出画的人"当成
       很远的人滤掉，正好推翻用户另一条拍板（第 2 条）。

    这一套口径的已知代价（如实记账，别到时候当成 bug 找）：
      · 主人自己坐得比阈值还远时，两张脸都过不了尺寸闸门 → 既不会收窗也不会自动恢复；
        笔记本与 iMac 的正常坐姿（50~90 厘米）远在阈值之上，不受影响。
      · "一个人离屏幕比主人还近"这种几何，被动摄像头分不出谁是主人，此时算不出第二个人。
      · 海报或屏幕里被切一半的人像从此也算第二个人（照片里的人像一直被当真脸，
        见对外说明第 3 条），机制上无法区分。
      · 窄视角机器上，3 米外且正好被切一半的人可能仍过得了打折后的弱闸门 ——
        这是"贴边也算"与"1.5 米才算"同时成立时的必然残差，取值推导写在常量旁边。
    """
    total = 0
    for ob in observations or []:
        try:
            conf = float(ob.confidence())
        except Exception:
            total += 1
            continue
        r = _box_rect(ob)
        if r is None:
            if conf >= conf_min:
                total += 1
            continue
        edge = _touches_edge(r)
        if conf < (edge_conf_min if edge else conf_min):
            continue
        floor = near_w_min * (FACE_GUARD_EDGE_SHRINK if edge else 1.0)
        if r[2] < floor:
            continue
        total += 1
    return total


class PresenceArbiter:
    """把"每次数到几张脸"翻译成"该收起 / 该恢复"。

    为什么要迟滞而不是"一见到两张脸就收"：
      误检是必然会发生的 —— 屏幕上的人像、身后走过的人、视频里的脸都会被 Vision
      当成真人检出（本机实测：程序图标 0 张脸，生成的双人照 2 张脸，18ms）。
      单次命中就收窗，用户会反复看到"行情自己消失了"，几次之后就会把这个功能关掉，
      保护反而归零。因此要求连续命中，且恢复也要求连续，两个方向都不许瞬变。

    为什么"0 张脸"不能推进恢复计数：
      人离开座位时画面里可能一张脸都没有。若把 0 当"只剩自己"，程序会在**无人的
      时候**把行情亮出来，下一个路过的人直接看到 —— 这与功能目的相反。
      恢复必须**确实看到一张脸**（也就是主人回到座位）。
    """

    def __init__(self, hide_hits=FACE_GUARD_HIDE_HITS,
                 restore_hits=FACE_GUARD_RESTORE_HITS,
                 tick_sec=FACE_GUARD_TICK_SEC,
                 max_idle_sec=FACE_GUARD_MAX_IDLE_SEC,
                 dwell_sec=FACE_GUARD_DWELL_SEC):
        self.hide_hits = int(hide_hits)
        self.restore_hits = int(restore_hits)
        self.tick_sec = float(tick_sec)
        self.max_idle_sec = float(max_idle_sec)
        # 驻留时长（用户原话：第二个人脸需停留在半径内 >0.38 秒才收窗，恢复同理反推）
        self.dwell_sec = float(dwell_sec)
        self.reset()

    def reset(self):
        self._hide_streak = 0
        self._restore_streak = 0
        self._two_since: Optional[float] = None
        self._one_since: Optional[float] = None
        self.hidden_by_guard = False
        self.last_faces: Optional[int] = None
        self.hide_at: Optional[float] = None
        self._idle_reported = False

    def clear_streaks(self):
        """只清"连击计数与驻留起点"，**保留**"我是因为第二张脸才被收起来的"这件事。

        为什么需要分开（复核 F1）：停摄像头的理由不止一种。用户关功能、手动缩小、
        看守超时 —— 这几种都该整体归零；但"采不到画面"这种中断不是用户改了主意，
        窗口还收在 Dock 里等着自动恢复。若连 `hidden_by_guard` 一起抹掉，30 秒后
        自愈重试时主窗会判定"没人被守卫收着、窗口又不可见 → 不用看守"，于是再停一次，
        自动恢复从此死掉 —— 而菜单上写的还是"等它松开"。
        驻留起点也必须一起清：中断前那一刻的"已经看了 0.3 秒"不能算进重连之后。
        """
        self._hide_streak = 0
        self._restore_streak = 0
        self._two_since = None
        self._one_since = None
        self.last_faces = None

    def feed(self, faces: int, now: Optional[float] = None) -> str:
        """喂一次计数，返回这一刻应做的动作（EVENT_*）。

        `now` 只为可测性存在（用例注入单调时钟），默认取 `time.monotonic()`。

        两个条件同时满足才动手：**连击满** + **驻留够久**（`dwell_sec`）。
        调用方必须保证"一份新样本只喂一次"（主窗按样本时刻去重），否则
        驻留计时会被同一份快照的重复喂入撑长 —— 那既骗过连击、也骗过驻留。
        """
        t = time.monotonic() if now is None else float(now)
        faces = max(0, int(faces))
        self.last_faces = faces

        if faces >= 2:
            if self._two_since is None:
                self._two_since = t
            self._one_since = None
            self._hide_streak += 1
            self._restore_streak = 0
        elif faces == 1:
            if self._one_since is None:
                self._one_since = t
            self._two_since = None
            self._restore_streak += 1
            self._hide_streak = 0
        else:                       # 0 张脸：两个计数都归零，见类文档第三条
            self._hide_streak = 0
            self._restore_streak = 0
            self._two_since = None
            self._one_since = None

        if (not self.hidden_by_guard
                and self._hide_streak >= self.hide_hits
                and self._two_since is not None
                and t - self._two_since >= self.dwell_sec):
            self.hidden_by_guard = True
            self.hide_at = t
            self._idle_reported = False
            self._hide_streak = 0
            self._restore_streak = 0
            self._two_since = None
            self._one_since = None
            return EVENT_HIDE

        if self.hidden_by_guard:
            if (self._restore_streak >= self.restore_hits
                    and self._one_since is not None
                    and t - self._one_since >= self.dwell_sec):
                self.hidden_by_guard = False
                self.hide_at = None
                self._idle_reported = False
                self._restore_streak = 0
                self._one_since = None
                return EVENT_RESTORE
            if (self.hide_at is not None
                    and not self._idle_reported
                    and t - self.hide_at >= self.max_idle_sec):
                # 只报一次：报完调用方会停掉摄像头，本状态机随即停止喂入。
                self._idle_reported = True
                return EVENT_IDLE
        return EVENT_NONE


def vision_request_succeeded(result) -> bool:
    """Normalize PyObjC's BOOL and (BOOL, NSError) return shapes."""
    return bool(result[0] if isinstance(result, tuple) else result)


def _load_frameworks() -> Tuple[Optional[Dict[str, Any]], str]:
    """动态装载 Vision + AVFoundation，返回所需类表。

    成功返回 (类表, "")；失败返回 (None, 原因)。
    失败**不抛异常**：非 macOS、系统框架被裁剪、桥接缺失都收敛成一个可读原因，
    由主窗把它显示到界面上（保护没在跑必须让人看见，不能装作有）。
    """
    try:
        import objc
        from Foundation import NSBundle
    except Exception as exc:
        return None, "PyObjC 桥接不可用: %r" % (exc,)

    for path in ("/System/Library/Frameworks/AVFoundation.framework",
                 "/System/Library/Frameworks/Vision.framework"):
        try:
            # load() 的返回值只在"本次首次装入"时为真：AVFoundation 常被系统别的
            # 框架提前装入，此时它返回 False 而类照样取得到。因此**不以返回值判成败**，
            # 统一以下面的 lookUpClass 结果为准。
            NSBundle.bundleWithPath_(path).load()
        except Exception as exc:
            return None, "框架装载失败 %s: %r" % (path, exc)

    wanted = {
        "AVCaptureSession": "AVCaptureSession",
        "AVCaptureDevice": "AVCaptureDevice",
        "AVCaptureDeviceInput": "AVCaptureDeviceInput",
        "AVCaptureVideoDataOutput": "AVCaptureVideoDataOutput",
        "VNImageRequestHandler": "VNImageRequestHandler",
        "VNDetectFaceRectanglesRequest": "VNDetectFaceRectanglesRequest",
        "NSObject": "NSObject",
    }
    cls: Dict[str, Any] = {}
    try:
        for key, name in wanted.items():
            got = objc.lookUpClass(name)
            if got is None:
                return None, "系统类缺失: %s" % name
            cls[key] = got
    except Exception as exc:
        return None, "系统类查找失败: %r" % (exc,)
    return cls, ""


_QUEUE_LOCK = threading.Lock()
_QUEUE = None

# 停会话时"摘委托"失败才用得到的暂存区：委托对象被框架侧以不持有的裸引用使用，
# 正常收口时第 ① 步就把它摘干净、永远不进这里；一旦摘失败，宁可一直留着强引用，
# 也不能让一个可能还在被投递帧的对象先被 GC 掉（那是随机崩溃，且只在关功能瞬间出现）。
# 用 list 而不是 set：sink 未实现 __hash__ 语义保证，重复入栈也无害。
_PARKED_SINKS: list = []
# 暂存区上限（复核 F5）：见 `_teardown` 里那段"正常路径一次都不会进来"的说明。
_PARKED_MAX = 8

# 采集队列的名字（全进程一条，见 `_dispatch_queue`）
_QUEUE_NAME = "sf.faceguard.capture"

# ---- 委托类：进程内只注册一次（v1.4.44，复核 F1）----
# PyObjC 的 ObjC 类注册是**不可逆**的：第二次用同名 `type()` 建类直接抛
# `SFPresenceSink is overriding existing Objective-C class`。本机实测（拿同一个
# objc 基类连建两次）：第 1 次 OK，第 2 次抛错。而原来的 `_make_sink` 每次
# `start()` 都新建一个 —— 后果是**人脸守卫每个进程只能成功启动一次**：任何一次
# 停→起（手动缩小后点 Dock 恢复、看守超时后恢复、摄像头被别的程序占用后自愈、
# 关了再开）之后功能永久失效，菜单只能显示那行英文异常，并且每 10 秒重试一次、
# 每次刷一行 ERROR（v1.4.41/42 承诺的"中断后能自愈"就是这么被挡死的）。
_SINK_CLASS: Any = None
_SINK_CLASS_LOCK = threading.Lock()
# owner 反查表：类只建一次，就不能再用闭包捕获某个 monitor（那会把每一个历史
# 实例永久钉在 ObjC runtime 里 —— 复核 F3）。改成按委托实例反查，拆卸时销账。
_SINK_OWNERS: dict = {}

# 授权框的去重（复核 A4，v1.4.45）：记在**进程**上而不是 monitor 实例上 —— 主窗每次
# 重启守卫都会新建实例，挂在实例上等于"换个实例就再弹一次"。
# v1.4.57 改成带冷却的时间戳，而不是 v1.4.45 的永久布尔：本机 16:36 的日志实证 ——
# 用户第一下点击真的请求了（requested），但系统弹窗没被看见（被置顶悬浮窗压住，
# 或者用户当时没理它），此后连点四下全部只得到 "asked"：**这个菜单项在这个进程里
# 永久变成空操作**，唯一出路是重启程序。用户看到的就是"点了没反应"。
# 冷却窗口（FACE_GUARD_REASK_COOLDOWN_SEC）保留原来"连点三下不叠三个框"的意图，
# 但错过一次弹窗之后再点，一定会把系统框重新叫出来。
# ⚠ 初值必须是 None 而不是 0.0：macOS 的 monotonic 从开机算起，开机头几秒 0.0
#   会被算成"刚刚问过"（§6 第 4 条那个单调时钟基线坑的又一形状）。
_ACCESS_ASKED_AT: "float | None" = None


def _friendly_error(exc: BaseException) -> str:
    """把框架异常翻译成一句能给人看的中文（复核 A3）。

    `repr(exc)` 是给排障的人看的：外部框架抛什么字符串不可控，直接放进菜单和
    诊断包，用户看不懂，也和"日志只允许整数计数"的口径不一致。原文**必须**
    仍然进日志（调用点都在 log 时带 %r），这里只负责界面/诊断包那一半。
    """
    text = repr(exc)
    if "is a block, but no signature" in text:
        return "系统摄像头框架缺少必要组件（无法弹出授权框）"
    if isinstance(exc, RuntimeError):
        return str(exc)          # 本模块自己抛的都是中文（如"摄像头输入通道建立失败"）
    if type(exc).__name__ == "error" and "objc" in type(exc).__module__:
        return "系统框架调用失败（ObjC 层报错，原文已写入日志）"
    if isinstance(exc, OSError):
        return "系统层面出错（原文已写入日志）"
    return "出现了一次无法识别的错误（原文已写入日志）"


def _presence_sink_class(ns_object: Any) -> Any:
    """取那个**只注册一次**的委托类（PyObjC 的类注册不可逆，第二次同名建类必抛）。

    回调里要找到"这个委托属于哪个 monitor"，两条路都走一遍再取：
      ① 实例属性 `_owner`（装配时挂上，实测可用）；
      ② `_SINK_OWNERS` 按委托实例反查（PyObjC 的 hash/eq 按 ObjC 指针，实测可用）。
    为什么要双路：由系统侧发起回调时拿到的 `self_ref` 可能是一个新取的代理对象，
    只靠其中一路有可能反查不到 —— 反查不到的表现是"绿灯亮着但一帧都没算过"，
    那是最难查的那种失效。两条都不通就直接返回（已拆卸的委托被补投一帧属正常）。
    """
    global _SINK_CLASS
    with _SINK_CLASS_LOCK:
        if _SINK_CLASS is not None:
            return _SINK_CLASS

        def on_frame(self_ref, output, sample_buffer, connection):
            owner = getattr(self_ref, "_owner", None)
            if owner is None:
                owner = _SINK_OWNERS.get(self_ref)
            if owner is None:
                return
            try:
                owner._on_frame(output, sample_buffer, connection)
            except Exception as exc:
                # 采集线程里抛出去就是崩溃，但绝不能变成"功能静默失效"：
                # log_swallow 会写 SWALLOWED 前缀（AGENTS §6 第 9 条）。
                log_swallow("sf_presence.on_frame", exc)

        _SINK_CLASS = type("SFPresenceSink", (ns_object,), {
            "captureOutput_didOutputSampleBuffer_fromConnection_": on_frame})
        return _SINK_CLASS


def _dispatch_queue():
    """取那条全进程共用的串行 GCD 队列，给 AVFoundation 投递帧。

    为什么用 ctypes 而不是 `import dispatch`：`dispatch` 的 PyObjC 桥接属于
    pyobjc-framework-os，本工程没装（也不想为这一个函数装）。libSystem 上的
    `dispatch_queue_create` 是稳定 ABI，直接取地址、再用
    `objc.objc_object(c_void_p=...)` 包成 ObjC 对象即可 —— 本机实测包出来的
    对象类型是 `OS_dispatch_queue_serial`，且
    `setSampleBufferDelegate_queue_` 愿意接受它。

    为什么复用而不是每次新建（复核 F6）：`dispatch_queue_create` 返回的是 +1 拥有权，
    而 PyObjC 的包装对象**不替我们管这个指针的生命周期**（没有 CoreMedia/dispatch
    桥接就没法安全 dispatch_release）。每次起会话都新建一条、只增不减，用户一天
    反复缩小/恢复几十次就攒下几十条队列。复用一条就没有这笔账，也不需要去猜释放时机。

    为什么不再收 name 参数（复核 F5）：复用之后只有第一次调用时的名字会真的生效，
    留着参数等于给后来人一个"每次能拿到自己那条"的错觉。
    """
    global _QUEUE
    with _QUEUE_LOCK:
        if _QUEUE is not None:
            return _QUEUE
        lib = ctypes.CDLL(None)                  # 已加载镜像里的 libdispatch
        lib.dispatch_queue_create.restype = ctypes.c_void_p
        lib.dispatch_queue_create.argtypes = [ctypes.c_char_p, ctypes.c_void_p]
        import objc
        ptr = lib.dispatch_queue_create(_QUEUE_NAME.encode("utf-8"), None)
        if not ptr:
            raise RuntimeError("dispatch_queue_create 返回 NULL")
        _QUEUE = objc.objc_object(c_void_p=ptr)
        return _QUEUE


def screen_unavailable() -> bool:
    """屏幕是否锁着、或主显示器是否在休眠（v1.4.56 省电判据）。

    只在**明确读到**「锁了 / 睡了」时才返回 True：框架缺失、查询抛错、
    键不存在一律返回 False。方向是刻意的 —— 误判「没锁」只是多耗电，
    误判「锁了」会让保护在用户回来时静默不工作（本仓反复防的那类）。

    v1.4.60（复核 F13）：四条退路一律保留 fail-open，但不再一声不吭 —— 按 §6 第 9 条
    走 log_swallow 留 SWALLOWED 痕。否则"省电停摄"在一台取不到 Quartz 的机器上永久
    失效，而日志里连一行交代都没有（这一族的判定节拍每 2 秒一次，log_swallow 自带限流）。
    """
    try:
        import Quartz
    except Exception as exc:
        log_swallow("sf_presence.screen_unavailable(Quartz)", exc)
        return False
    try:
        d = Quartz.CGSessionCopyCurrentDictionary()
        if d is not None and int(d.get("CGSSessionScreenIsLocked", 0) or 0) == 1:
            return True
    except Exception as exc:
        log_swallow("sf_presence.screen_unavailable(session)", exc)
        return False
    try:
        return bool(Quartz.CGDisplayIsAsleep(Quartz.CGMainDisplayID()))
    except Exception as exc:
        log_swallow("sf_presence.screen_unavailable(display)", exc)
        return False


def camera_access_status() -> str:
    """返回摄像头授权状态的可读名，不触发任何系统弹窗。

    AVCaptureDeviceAuthorizationStatus：0 undetermined / 1 restricted /
    2 denied / 3 authorized。
    """
    cls, _ = _load_frameworks()
    if cls is None:
        return STATE_UNSUPPORTED
    try:
        code = int(cls["AVCaptureDevice"].authorizationStatusForMediaType_(_media_video()))
    except Exception as exc:
        log_line("face_guard auth status failed: %r" % (exc,), "WARNING")
        return STATE_UNSUPPORTED
    return {0: "undetermined", 1: STATE_RESTRICTED,
            2: STATE_DENIED, 3: "authorized"}.get(code, "unknown:%d" % code)


def _camera_devices(cls: Dict[str, Any]):
    """取默认摄像头设备；拿不到视频介质常量时退回全量枚举。"""
    dev = cls["AVCaptureDevice"]
    media = _media_video()
    try:
        d = dev.defaultDeviceWithMediaType_(media)
        if d is not None:
            return d
    except Exception as exc:
        log_line("face_guard default device by type failed: %r" % (exc,), "WARNING")
    try:
        devs = list(dev.devicesWithMediaType_(media) or [])
    except Exception:
        # 介质字面量在这台系统上不被接受时，全量枚举里按"是视频设备"筛。
        try:
            devs = list(dev.devices() or [])
        except Exception as exc:
            log_line("face_guard device enumeration failed: %r" % (exc,), "WARNING")
            devs = []
    return devs[0] if devs else None



class FacePresenceMonitor:
    """摄像头取帧 + Vision 数脸，只对外暴露"最近一次计数"。

    线程模型（这条是设计前提，改动时别破）：
      · Vision 一律在采集线程里跑，**绝不碰 Qt**；
      · 主线程只用 `snapshot()` 读一份 `(计数, 采样时刻)` 只读快照；
      · 因此主窗的迟滞判定、收起与恢复全部发生在 Qt 主线程，跨线程只共享一个
        整数元组，由 `self._lock` 保护。历史上本仓有过"从 Qt 伸手进 AppKit"
        导致 offscreen SIGSEGV 的事故（v1.4.33 收口），这里刻意保持单向。
    """

    def __init__(self, tick_sec: float = FACE_GUARD_TICK_SEC,
                 conf_min: float = FACE_GUARD_CONF_MIN,
                 on_sample=None):
        self.tick_sec = float(tick_sec)
        self.conf_min = float(conf_min)
        # 数到新快照时被调一次（在**采集线程**里）。主窗用它把判定从"等轮询对齐"
        # 变成"数完就判"；回调必须是线程安全的，这里只允许 emit 一个跨线程 Qt 信号。
        # 传 None 就是原来的行为（纯靠主线程那张表去读快照）。
        self._on_sample = on_sample
        self.state = STATE_OFF
        self.last_error = ""
        # 两个整数计数器，专门回答"帧到底来没来、来了认不认得出脸"（v1.4.50）。
        # 为什么只放整数：隐私硬约束第 1 条 —— 帧数据不许落地，而日志/诊断包必须
        # 能在事后一句话回答这两问。v1.4.49 之前的实况是：`performRequests_error:`
        # 的返回值被直接丢掉，Vision 又没有 PyObjC 元数据（本机实测注册 out-arg
        # 元数据仍只返回 bool），失败时既拿不到 NSError、也不留任何痕迹 —— 采集线程
        # 一切正常、界面写"运行中"、却一张脸都数不出来，用户和我们都无从分辨。
        self.frames = 0                 # 成功计数并写进快照的帧数
        self.detect_errors = 0          # 帧来了但检测失败的次数（含异常与返回 False）
        self._session = None
        self._delegate = None
        self._input = None
        self._output = None
        self._cls: Optional[Dict[str, Any]] = None
        self._lock = threading.Lock()
        # start/stop 串行化：AVCaptureSession.startRunning() 是会阻塞的（首次拉起
        # 实测数百毫秒），必须能放到后台线程去做，同时与主线程的 stop() 互斥。
        self._api_lock = threading.Lock()
        self._stop_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._stop_event.set()
        self._stop_inflight = False
        self._stop_result = True
        self.stop_timeouts = 0
        self._starting = False
        # 会话代号：每次 stop() 递增。用来作废"排队等锁期间用户已经关掉守卫"的那些启动
        # （详见 request_start 的 docstring；S1 那枚 P0 的落点就在这个计数上）。
        self._session_gen = 0
        self._access_asked = False
        self._sample: Tuple[int, float] = (0, 0.0)
        # 最近一帧里最大那张达标脸占画面宽的比例（标定"几成 = 几米"用，见常量注释）
        self.last_face_width = 0.0
        self._last_detect = 0.0
        # v1.4.56 加急复检的账本：上一帧数到几张、本秒还剩几次加急额度。
        self._prev_count: Optional[int] = None
        self._expedite_left = int(FACE_GUARD_EXPEDITE_BUDGET)
        self._expedite_window = 0.0
        self.expedited = 0        # 累计加急次数（诊断包用，证明这条真的在跑）
        # 会话进入 RUNNING 的时刻。为什么单独记（复核 F2）：`startRunning` 在设备被
        # 别的程序占用、或权限还没给的时候**照样返回"在跑"却一帧都不发**（本机实测
        # isRunning=True 而零帧）。只看"最后一帧的时刻"会永远等到 0，超时判定形同虚设，
        # 所以零帧时改用这个基线来判"保护其实没在跑"。
        self.running_since = 0.0

    # ---- 对外 ----
    def snapshot(self) -> Tuple[int, float]:
        """(第几张脸的计数, 采样用的单调时刻)。主线程调用。"""
        with self._lock:
            return self._sample

    def request_start(self) -> str:
        """非阻塞拉起：把真正的采集会话建立过程交给后台线程，立刻返回 STARTING。

        为什么必须这样：调用点是 Qt 主线程（右键菜单勾选、窗口显示后的同步），
        而 startRunning() 在首次拉起时会占住调用线程数百毫秒 —— 用户点一下菜单
        界面就卡住，比任何误检都更让人想关掉这个功能。状态由主线程的轮询表去读。

        ⚠ 代号（session generation）：worker 拿到 `_api_lock` 可能已经是一两秒以后，
        这期间用户完全可能又点了一次「关闭」。旧实现里 `stop()` 只把 state 写成 OFF，
        而 `_start_locked` 只拦 `state==RUNNING` —— 晚到的 worker 照样把会话起来，
        于是**关掉守卫之后菜单栏绿灯过一会儿自己亮起，而且再也停不掉**（主窗这时
        已经把这个 monitor 丢弃成 None，没人再去 stop 它）。现在把发起时刻的代号交给
        worker，拿到锁之后先比对，不符即作废。
        """
        if self.state == STATE_RUNNING or self._starting:
            return self.state
        self._starting = True
        gen = self._session_gen

        def _worker():
            try:
                self.start(gen)
            finally:
                self._starting = False

        try:
            threading.Thread(target=_worker, name="sf-faceguard-start",
                             daemon=True).start()
        except Exception as exc:
            # No worker means its finally block cannot release the starting latch.
            self._starting = False
            self.state = STATE_FAILED
            self.last_error = _friendly_error(exc)
            log_swallow("sf_presence.request_start", exc)
            return self.state
        return STATE_STARTING

    def is_starting(self) -> bool:
        return bool(self._starting)

    def start(self, gen: "int | None" = None) -> str:
        """阻塞式拉起采集；返回最终状态名。失败不抛异常，状态可直接显示给用户。

        ⚠ 主线程请调用 `request_start()`，不要直接调本方法。
        """
        with self._api_lock:
            return self._start_locked(gen)

    def _start_locked(self, gen: "int | None" = None) -> str:
        if self.state == STATE_RUNNING:
            return self.state
        if gen is not None and gen != self._session_gen:
            # 排队等锁的这段时间里守卫被关过（stop 递增了代号）—— 这次启动作废。
            log_line("face_guard 启动作废：等锁期间已被关闭（代号 %s→%s）"
                     % (gen, self._session_gen))
            self.state = STATE_OFF
            return self.state
        cls, why = _load_frameworks()
        if cls is None:
            self.state = STATE_UNSUPPORTED
            self.last_error = why
            log_line("face_guard unsupported: %s" % why, "WARNING")
            return self.state
        self._cls = cls

        self.state = STATE_STARTING
        # 先查授权，再枚举摄像头（2026-10-21 调序）。没有授权就起不了会话，枚举只是白付代价：
        # 实测启动后约 2.5 秒这里首次枚举时，AVFoundation 往主队列投了一个等设备列表的块，
        # 主线程停 0.2~0.55 秒；未授权的用户还会每约 2 秒重试一次，每次都先枚举。
        # 读不出授权状态时 camera_access_status 返回 unsupported，照旧落到下面的枚举，行为不变。
        status = camera_access_status()
        if status in (STATE_DENIED, STATE_RESTRICTED):
            # 不静默降级成"看起来开了其实没跑"：状态直接给界面显示，并留日志。
            self.state = status
            self.last_error = "摄像头权限为 %s" % status
            log_line("face_guard %s" % status, "WARNING")
            return self.state
        if status == "undetermined":
            # **不再在启动时向系统请求权限**（2026-10-09 用户明确要求"去掉这个弹窗提示"）。
            # 旧行为的事实：产物是 ad-hoc 签名，每次重装/更新后 TCC 把它当成新程序，
            # 授权状态回到 undetermined，于是每次更新后第一次启动都弹一次系统框 ——
            # 用户什么都没做却被要求当场做个决定。v1.4.45 把"只问一次"收成进程级，
            # 只解决了"同一进程里反复弹"，没解决"每次更新都弹"。
            # 新行为：这里只报告状态、不发起请求；请求改由用户点菜单项的那一刻调用
            # `request_access_once()` —— 这才是原来那句注释想要的"在用户能看见弹窗的
            # 时候发起"。状态行与诊断包必须把原因说出来，不能"看起来开了其实没跑"。
            self.state = STATE_NEEDS_AUTH
            # 这一行会被界面直接显示，所以指的路径必须是屏幕上真实存在的那一项。
            self.last_error = ("还没请求过摄像头权限：点一次菜单里的「%s」就会问你"
                               % FACE_GUARD_MENU_TEXT)
            log_line_throttled("face_guard.needs_auth",
                               "face_guard 未授权：不主动弹系统框，等用户在菜单里主动开启时再请求",
                               level="WARNING", every=30)
            return self.state

        device = _camera_devices(cls)
        if device is None:
            self.state = STATE_NO_CAMERA
            self.last_error = "没有可用的摄像头设备"
            log_line("face_guard no camera", "WARNING")
            return self.state

        try:
            self._build_session(cls, device)
        except Exception as exc:
            self.state = STATE_FAILED
            self.last_error = _friendly_error(exc)
            self._teardown()
            # 同一异常反复出现时不逐次刷 ERROR（复核 F2）：主窗对没起来的会话按
            # 10 秒节拍重试，逐条记录一天能写 8640 行，把真实故障挤出 7 天轮转。
            # 首条必记，其后每 30 次记一条并附累计数（sf_logging 的现成节流）。
            log_line_throttled("face_guard.session_failed",
                               "face_guard session FAILED: %r" % (exc,),
                               level="ERROR", every=30)
            return self.state
        self.state = STATE_RUNNING
        self.running_since = time.monotonic()
        # 计数器按会话归零：诊断包与状态行说的是"这一次会话"的帧，不能把上一段
        # 会话的数字混进来（否则停→起之后，界面会拿着几小时前的一次成功说"有帧"）。
        with self._lock:
            self.frames = 0
            self.detect_errors = 0
        log_line("face_guard running tick=%.1fs conf=%.2f" % (self.tick_sec, self.conf_min))
        return self.state

    def stop(self, wait: bool = True) -> bool:
        """停采集并释放会话；计数清零。返回值 = 是否在本次调用里同步停完。

        wait=True：退出收尾用。进程要走了，必须真把会话停掉，菜单栏绿灯才会灭。
        wait=False：交互路径用（用户点缩小、关掉功能、中断后自愈）。首次拉起会话
        是在后台线程里做的、实测占数百毫秒，`_api_lock` 被它占住时主线程**不能等** ——
        等在这里就变成"用户点一下缩小，界面卡半秒"（复核 F5）。这时改丢一个后台
        线程去做，本函数立刻返回 False；那个线程持有本对象的引用，不会半路被回收。

        ⚠ 返回 False 的含义是"**还没停**"，不是"停好了"。调用方因此**不许就地丢弃本
        monitor**：那个补停的线程是 daemon，进程一退就被硬杀，会话于是永远没停 ——
        绿灯不灭，而诊断包 `camera_held` 会说谎。主窗的收口是把这类未完成的停挂进
        `_face_guard_pending_stop`，退出时逐个 `stop(wait=True)`（S1 第二处）。
        """
        # 先递增代号再动手：排队等锁的那次启动必须**立刻**作废，不能等 teardown 真跑完
        self._session_gen += 1
        if (self._session is None and self._output is None and self._delegate is None
                and not self._starting and self._api_lock.acquire(blocking=False)):
            try:
                return self._stop_locked()
            finally:
                self._api_lock.release()
        with self._stop_lock:
            if not self._stop_inflight:
                self._stop_event = threading.Event()
                self._stop_inflight = True
                self._stop_result = False
                try:
                    threading.Thread(target=self._deferred_stop,
                                     name="sf-faceguard-stop", daemon=True).start()
                except Exception as exc:
                    self._stop_inflight = False
                    self._stop_event.set()
                    log_swallow("sf_presence.stop(deferred worker)", exc)
            event = self._stop_event
        if wait and not event.wait(0.25):
            self.stop_timeouts += 1
            return False
        return event.is_set() and self._stop_result

    def _deferred_stop(self):
        try:
            with self._api_lock:
                self._stop_result = self._stop_locked()
        except Exception as exc:
            log_swallow("sf_presence._deferred_stop", exc)
        finally:
            with self._stop_lock:
                self._stop_inflight = False
                self._stop_event.set()

    def _stop_locked(self) -> bool:
        self._teardown()
        self.state = STATE_OFF
        self.last_error = ""
        # 停表即清零：计数器只描述"当前这一段会话"，停着的时候不该还能读到上一段
        # 会话的帧数（界面与诊断包会在停表后继续问这两个数）。
        with self._lock:
            self.frames = 0
            self.detect_errors = 0
            self._sample = (0, 0.0)
        return True

    # ---- 内部 ----
    def request_access_once(self) -> str:
        """唯一的授权发起点：**用户主动**点菜单开启时才调，程序自己永不调用。

        返回值是给调用方看事实用的：
          · "requested" —— 这一次真的向系统发了请求（系统框会出现）；
          · "asked"     —— 刚问过还没到冷却窗口，不再重复问（否则一次点击连着弹三个框）；
          · 其它        —— 当前状态原样返回（authorized / denied / restricted / …），不需要问。

        v1.4.57：冷却到点之后**必须能再问一次**。旧实现是进程级永久布尔，
        系统框被错过一次（压在置顶窗后面没看见 / 当时没理它），这个菜单项在本进程
        里就永久变成空操作 —— 本机 16:36 的日志就是这样连点四下全无反应。
        """
        global _ACCESS_ASKED_AT
        status = camera_access_status()
        if status != "undetermined":
            self.state = status if status in (STATE_RUNNING,) else STATE_STARTING
            return status
        now = time.monotonic()
        last = _ACCESS_ASKED_AT
        if last is not None and (now - last) < FACE_GUARD_REASK_COOLDOWN_SEC:
            self.state = STATE_STARTING
            self.last_error = ("已经向系统请求过摄像头权限，请在弹窗里作答；"
                               "弹窗没看见就稍后再点一次（约 %.0f 秒后可以重问）"
                               % FACE_GUARD_REASK_COOLDOWN_SEC)
            return "asked"
        self.state = STATE_STARTING
        self._request_access()
        if self.state in (STATE_UNSUPPORTED, STATE_FAILED):
            return self.state
        _ACCESS_ASKED_AT = now
        self._access_asked = True
        self.last_error = "等待用户在系统弹窗里允许摄像头"
        return "requested"

    def _request_access(self):
        """发起系统授权弹窗。**这一步必须有 pyobjc-framework-AVFoundation 才行**。

        实测（本机 2026-10-08）：只装 pyobjc-core 时，
        `requestAccessForMediaType_completionHandler_` 的第三个参数是 ObjC block，
        PyObjC 没有元数据就直接 `TypeError: Argument 3 is a block, but no signature
        available`。三条"不加依赖"的绕法全部试过并失败：
          · 传 None 当 handler → 调用不报错，但系统**不弹窗**、状态永远停在
            undetermined（等于功能永远起不来，还以为在等用户点）；
          · `objc.callbackFor(...)` 装饰 Python 函数 → `ValueError: No callback
            argument in the specified object`（它要求方法签名里已带 block 元数据）；
          · `objc.lookUpClass` 动态取类 → 类能取到，签名元数据依然缺。
        装上桥接后同一段调用立刻成功（回调抵达 granted=False，因为本机这次是
        裸 python 进程、没有 app bundle 身份，TCC 直接判不允许 —— 弹窗与否
        只能在装机版里验）。
        所以这里在桥接缺失时明确落到 UNSUPPORTED 并写清原因，而不是让用户对着
        一个永远不会出现的弹窗干等。
        """
        try:
            # 只为注册 AVFoundation 的方法元数据（block 签名），不取它的符号
            import AVFoundation                            # noqa: F401
        except Exception as exc:
            self.state = STATE_UNSUPPORTED
            self.last_error = "缺少 pyobjc-framework-AVFoundation，无法弹授权框: %r" % (exc,)
            log_line("face_guard cannot request access: AVFoundation bridge missing", "ERROR")
            return
        cls = self._cls
        if cls is None:
            cls, why = _load_frameworks()
            if cls is None:
                self.state = STATE_UNSUPPORTED
                self.last_error = why
                log_line("face_guard cannot request access: %s" % why, "ERROR")
                return
            self._cls = cls
        try:
            cls["AVCaptureDevice"].requestAccessForMediaType_completionHandler_(
                _media_video(), lambda granted: log_line(
                    "face_guard permission granted=%r" % bool(granted)))
        except Exception as exc:
            self.state = STATE_FAILED
            self.last_error = _friendly_error(exc)
            log_line("face_guard permission request failed: %r" % (exc,), "ERROR")

    def _build_session(self, cls: Dict[str, Any], device):
        session = cls["AVCaptureSession"].alloc().init()
        if session is None:
            raise RuntimeError("AVCaptureSession 构造失败")
        # Own partial resources before any native call can fail: teardown must detach before release.
        self._session = session
        self._apply_power_profile(session, device)
        # PyObjC 对"带 NSError 出参"的构造方法返回 **(对象, error) 元组**（本工程装了
        # pyobjc-framework-AVFoundation，`deviceInputWithDevice:error:` 有元数据 → 走这条形状）。
        # 把元组直接交给 canAddInput_ 会被桥接成 NSArray，AVFoundation 反过来向它发
        # `-session`，抛 NSInvalidArgumentException。
        # ⚠ 这不是回归，是**这个功能自 v1.4.40 上线以来第一次真正尝试建会话**就撞上的死路：
        #   实机日志里 `face_guard permission granted=True` 历史上只出现过一次
        #   （2026-10-09 10:33 的 v1.4.46），此前从未有 run 走到这一步 ——
        #   单测全部用假类，而假类返回值是造出来的，不会呈现 PyObjC 的元组形状，
        #   所以"每个假类都返回单个对象"的测试恰好把这条路径整体测不到（台账 E02）。
        #   回归用例见 tests/test_v1449_face_session.py：故意让桩返回元组。
        res = cls["AVCaptureDeviceInput"].deviceInputWithDevice_error_(device, None)
        input_port, input_err = res if isinstance(res, tuple) else (res, None)
        if input_port is None:
            # 旧写法是 `if not input_port` —— 对元组恒为假，等于这条保护从来没生效过。
            raise RuntimeError("摄像头输入通道建立失败（设备可能被别的进程占用）：%s"
                               % (input_err or "系统未给出原因"))

        if not session.canAddInput_(input_port):
            raise RuntimeError("系统拒绝加入摄像头输入通道")
        self._input = input_port
        session.addInput_(input_port)

        output = cls["AVCaptureVideoDataOutput"].alloc().init()
        if output is None:
            raise RuntimeError("视频输出通道构造失败")
        self._output = output
        delegate = self._make_sink(cls)
        self._delegate = delegate
        # alwaysDiscardsLateVideoFrames：宁可得不到帧，也不要队列堆出延迟 ——
        # 我们要的是"最近有没有两张脸"，不是每一帧。
        output.setAlwaysDiscardsLateVideoFrames_(True)
        queue = _dispatch_queue()
        output.setSampleBufferDelegate_queue_(delegate, queue)
        if not session.canAddOutput_(output):
            raise RuntimeError("系统拒绝加入视频输出通道")
        session.addOutput_(output)
        # 刻意不去改 AVCaptureConnection.videoOrientation：Mac 内置摄像头的帧本身
        # 就是正立的，VNImageRequestHandler 会读 sample buffer 自带的方向信息。
        # 手工写一个朝向反而可能把帧转成 90°，而 Vision 对旋转过的脸检出率会掉 ——
        # 真要出问题（实机验证时数不到脸）再按实测结果决定是否需要显式朝向。
        session.startRunning()

    def _apply_power_profile(self, session, device) -> None:
        """把采集的功耗画像压到"数脸够用"的档位（v1.4.54，先实测再动）。

        两件事，都**fail-open**：设备不支持就记 WARNING 照旧用默认值 —— 省电不能
        把会话弄挂，"保护没了"比"多耗电"严重得多。

        ① 分辨率 640x480：离线实测两人照在 480x360 下置信度仍 0.82/0.81，与
           1024x768 完全持平 —— Vision 内部自己缩放，分辨率不是检测质量的瓶颈；
           像素吞吐比默认 High(1080p) 降约 4.4 倍。
        ② 传感器帧率上限 15fps：本机实测只支持 15.00~30.00fps，1/10s 会被系统
           当场拒绝（NSInvalidArgumentException）—— 所以这个值必须落在设备报告的
           支持区间里，写死一个"理论更省"的值就是把会话弄挂。
           `lockForConfiguration_` 带 NSError 出参，PyObjC 返回 (BOOL, error) 元组
           （v1.4.49 建会话栽过的同一种形状，这里当场解包）。

        fail-open 的每一档都会把"实际生效值"留在 `self.applied_profile`（诊断导出
        读取）：不支持时那支只写一行 WARNING，会被日志轮转挤掉 —— 远程从此分不清
        "按 640×480 省电采集"和"设备不支持、按默认档在跑"。
        """
        applied = {"preset": FACE_GUARD_CAMERA_PRESET, "preset_applied": False,
                   "fps": FACE_GUARD_CAMERA_FPS, "fps_applied": False}
        self.applied_profile = applied
        try:
            import AVFoundation                            # noqa: F401 只为取常量
            preset = str(AVFoundation.AVCaptureSessionPreset640x480)
        except Exception:
            preset = FACE_GUARD_CAMERA_PRESET              # 字面量兜底，值相同
        try:
            if session.canSetSessionPreset_(preset):
                session.setSessionPreset_(preset)
                applied["preset"] = preset
                applied["preset_applied"] = True
            else:
                log_line("face_guard 相机不支持 %s，按默认档采集" % preset, "WARNING")
        except Exception as exc:
            log_line("face_guard 设置采集档位失败（按默认档继续）：%r" % (exc,), "WARNING")
        try:
            from CoreMedia import CMTimeMake
        except Exception as exc:
            log_line("face_guard CoreMedia 桥接缺失，帧率维持默认：%r" % (exc,), "WARNING")
            return
        res = None
        try:
            res = device.lockForConfiguration_(None)
        except Exception as exc:
            log_line("face_guard 设备配置锁获取失败（帧率维持默认）：%r" % (exc,), "WARNING")
            return
        locked = res[0] if isinstance(res, tuple) else res
        if not locked:
            log_line("face_guard 设备配置锁被拒（帧率维持默认）", "WARNING")
            return
        try:
            dur = CMTimeMake(1, int(FACE_GUARD_CAMERA_FPS))
            device.setActiveVideoMinFrameDuration_(dur)
            device.setActiveVideoMaxFrameDuration_(dur)
            applied["fps_applied"] = True
        except Exception as exc:
            # 本机实测 1/10s 会被拒（设备只报 15~30fps）——这类拒绝必须留痕但绝不外抛。
            log_line("face_guard 帧率上限 %dfps 被设备拒绝（维持默认）： %r"
                     % (FACE_GUARD_CAMERA_FPS, exc), "WARNING")
        finally:
            try:
                device.unlockForConfiguration()
            except Exception as exc:
                log_swallow("sf_presence._apply_power_profile.unlock", exc)

    def _make_sink(self, cls: Dict[str, Any]):
        """造一个响应 captureOutput:didOutputSampleBuffer:fromConnection: 的委托。

        类是**进程内唯一**的（见 `_presence_sink_class` 与 `_SINK_CLASS` 的实测说明：
        第二次同名注册会抛，导致本功能每个进程只能起来一次）。owner 挂在委托实例上
        并同步登记反查表，**不用闭包捕获 self** —— 闭包会把历史上每一个 monitor
        永久钉在 ObjC runtime 里，与 v1.4.42 声称的"消除资源累积"正好相反。

        为什么仍然用 `type()` 动态建类而不是模块级 class 语句：后者要求在类定义时
        就把 AVFoundation 装进进程，而本模块在启动路径上就会被导入 —— 不该让没开
        这个功能的用户也付一次框架装载。实测动态建的实例
        `respondsToSelector_(captureOutput:didOutputSampleBuffer:fromConnection:)`
        为 True，而 AVFoundation 正是按 respondsToSelector 投递，
        不要求正式的协议声明。
        """
        sink = _presence_sink_class(cls["NSObject"]).new()
        try:
            sink._owner = self
        except Exception as exc:
            # 挂不上只是少了第一条反查路，还有 `_SINK_OWNERS` 兜着 —— 但必须留痕，
            # 否则哪天 PyObjC 改了代理行为，表现就是"绿灯亮着却数不到脸"。
            log_swallow("sf_presence._make_sink(挂 owner)", exc)
        _SINK_OWNERS[sink] = self
        # 当场就把委托记到自己身上：`_build_session` 后面还会再赋一次同样的值，
        # 但如果在 `addOutput`/`startRunning` 那几步抛了，只有这里记下的引用能让
        # `_teardown` 把反查表销账 —— 否则失败的会话会留下一个没人认领的 owner 条目。
        self._delegate = sink
        return sink

    def _on_frame(self, output, sample_buffer, connection):
        """采集线程的一拍：把帧交给 Vision，只留下一个整数计数。

        失败必须留痕（v1.4.50，台账 E02 复发的那一条）：这一段跑在采集线程里，
        任何"return 而不记"都表现为**界面写着运行中、绿灯亮着、却永远不会收窗**，
        事后翻日志一个字都查不到。三类失败各有各的计数器：
          · 帧是空的 / handler·req 构造不出来 / `performRequests:` 返回 NO / 抛异常
            → `detect_errors`；
          · 真数完一次脸 → `frames`，首帧额外写一行 INFO。
        这两条合起来让日志一句话回答"帧到底来没来"：`running` 有而首帧没有 =
        会话没发帧（设备被占用）；首帧有但 `detect_errors` 一直涨 = Vision 侧坏了。
        Vision 元数据缺失时返回 bool，带 NSError 出参元数据时返回 (bool, error)。
        两种形状都只按成功位判定，不把失败元组的真值当成成功帧。
        """
        generation = self._session_gen
        if not self._frame_generation_active(generation):
            return
        now = time.monotonic()
        if now - self._last_detect < self.tick_sec:
            return                  # 按节拍丢弃，占空比压到 ~2%
        self._last_detect = now
        cls = self._cls
        if cls is None or sample_buffer is None:
            self._count_detect_error("空帧（sample buffer 为 None）", generation)
            return
        try:
            req = cls["VNDetectFaceRectanglesRequest"].alloc().init()
            handler = cls["VNImageRequestHandler"].alloc().initWithCMSampleBuffer_options_(
                sample_buffer, None)
            if handler is None or req is None:
                self._count_detect_error("Vision 请求或 handler 构造失败", generation)
                return
            if not vision_request_succeeded(handler.performRequests_error_([req], None)):
                self._count_detect_error("VNImageRequestHandler 拒绝这一帧", generation)
                return
            observations = list(req.results() or [])
        except Exception as exc:
            # 采集线程里抛出去就是崩溃，但绝不能变成"功能静默失效"：
            # 计数器 + log_swallow 双记，日志里能看到"第几张脸都没数成"。
            if self._count_detect_error(repr(exc), generation):
                log_swallow("sf_presence._on_frame.detect", exc)
            return
        # 帧数据到此为止：只留一个整数。
        count = count_faces(observations, self.conf_min)
        # 标定用的一根浮点棒子：画面里最大那张达标脸占画面宽的比例。
        # 「1.5 米」这条闸门要靠镜头视角才能换成米，而视角在这台机器上读不到
        # （见 FACE_GUARD_NEAR_FACE_W 旁边那段），所以把比例留在诊断包里，
        # 配合量出来的坐姿距离就能反解视角。它不含任何画面内容。
        widest = max_face_width(observations)
        first = False
        with self._lock:
            # Detection can finish after stop/restart; publish only into its own session.
            if not self._frame_generation_active(generation):
                return
            self._sample = (count, now)
            self.last_face_width = widest
            self.frames += 1
            first = self.frames == 1
        if self._arm_expedite(now, count):
            with self._lock:
                self.expedited += 1
        # 通知主线程"有新样本了"（只 emit 一个排队信号，绝不在采集线程里碰 Qt 对象）。
        # 回调抛异常绝不能带走这一帧的计数，也不能把采集线程炸掉 —— 留痕优先。
        cb = self._on_sample
        if cb is not None:
            try:
                cb()
            except Exception as exc:
                log_swallow("sf_presence._on_frame.notify", exc)
        if first:
            # 这一行是"整条采集链真的通了"的锚点：AVFoundation 发帧 →
            # CMSampleBuffer 进 Vision → 数出脸。日志里搜 `face_guard 首帧` 就知道
            # 卡在哪一段；v1.4.40~1.4.49 期间这条锚点不存在，所以每一轮都只能靠猜。
            log_line("face_guard 首帧已计数：%d 张脸" % count)

    def _arm_expedite(self, now: float, count: int) -> bool:
        """计数变了就把下一次检测提前（v1.4.56，见常量旁注释）。

        只在**变化**时提前，稳态仍是 0.5 秒一拍；每秒最多
        `FACE_GUARD_EXPEDITE_BUDGET` 次，防止两个人来回走动时把占空比顶满。
        """
        prev = self._prev_count
        self._prev_count = count
        if now - self._expedite_window >= 1.0:
            self._expedite_window = now
            self._expedite_left = int(FACE_GUARD_EXPEDITE_BUDGET)
        if prev is None or count == prev or self._expedite_left <= 0:
            return False
        self._expedite_left -= 1
        # 让下一帧在 EXPEDITE 秒后就有资格被处理（而不是等满一个节拍）。
        self._last_detect = now - self.tick_sec + FACE_GUARD_EXPEDITE_SEC
        return True

    def _frame_generation_active(self, generation: int) -> bool:
        return (generation == self._session_gen
                and self.state in (STATE_STARTING, STATE_RUNNING))

    def _count_detect_error(self, why: str, generation: "int | None" = None) -> bool:
        """检测失败计数 + 节流留痕。只写字符串原因与整数，不碰帧数据。"""
        with self._lock:
            if generation is not None and not self._frame_generation_active(generation):
                return False
            self.detect_errors += 1
            n = self.detect_errors
        log_line_throttled("face_guard.detect_failed",
                           "face_guard 检测失败（第 %d 次）：%s" % (n, why),
                           level="WARNING", every=30)
        return True

    def _teardown(self):
        """停会话。三步顺序是**硬要求**，别调换（复核 F4）：

          ① 先把委托从输出通道上摘下来；
          ② 再 stopRunning()（**无条件执行**，见下面"摘不下来"那段）；
          ③ 最后才放掉我们对 delegate / session 的引用。

        为什么：`AVCaptureVideoDataOutput.sampleBufferDelegate` 是**不持有**的引用，
        而委托对象的生命周期由 Python 侧说了算。若先 stopRunning 再丢引用，投递队列上
        已经排好的帧回调仍可能在对象被回收之后抵达 —— 那是悬垂指针，表现成随机崩溃，
        而且只在"刚关掉功能的一瞬间正好有帧在飞"时出现，事后完全查不到。

        摘委托失败时（第 ① 步抛异常）怎么办：仍然要 stopRunning —— 跳过它就是
        "摄像头停不下来、绿灯常灭不掉"，比崩溃风险更贴近用户。但**不能**丢委托引用：
        把它挂进模块级 `_PARKED_SINKS` 暂存，绝不让一个可能还在被投递帧的对象被回收。
        这条兜底路径正常情况一次都不会走到，所以不担心它涨内存。
        """
        output = self._output
        detached = True
        if output is not None:
            try:
                output.setSampleBufferDelegate_queue_(None, None)
            except Exception as exc:
                detached = False
                log_line("face_guard detach delegate failed: %r" % (exc,), "WARNING")
        session = self._session
        if session is not None:
            try:
                session.stopRunning()
            except Exception as exc:
                log_swallow("sf_presence._teardown", exc)
        self._session = None
        self._input = None
        self._output = None
        # 反查表跟着拆卸销账（在清空 `_delegate` 之前做）：留着就等于把每一个历史
        # monitor 永久挂在进程上 —— 复核 F3 说的就是这种累积。摘失败的兜底暂存
        # （`_PARKED_SINKS`）仍然拿得住对象本身，只是它再收到补投的帧会安静丢掉。
        _SINK_OWNERS.pop(self._delegate, None)
        if self._delegate is not None:
            try:
                # The fallback owner route must also disappear for parked sinks.
                self._delegate._owner = None
            except Exception as exc:
                log_swallow("sf_presence._teardown(clear owner)", exc)
        if detached:
            self._delegate = None
        elif self._delegate is not None:
            # 兜底暂存要有上限：正常路径一次都不会进到这里，真反复失败说明这台机器的
            # AVFoundation 状态已经不对，继续囤对象只会掩盖问题、还占着内存。
            if len(_PARKED_SINKS) >= _PARKED_MAX:
                log_line("face_guard 暂存区已满 %d，丢弃最旧一个（摘委托反复失败）"
                         % _PARKED_MAX, "WARNING")
                del _PARKED_SINKS[0]
            _PARKED_SINKS.append(self._delegate)
            self._delegate = None
        with self._lock:
            self._sample = (0, 0.0)
        self._last_detect = 0.0
        self.running_since = 0.0
